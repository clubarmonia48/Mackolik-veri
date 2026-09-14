from flask import Flask, jsonify, send_from_directory, request
import requests, re, os, time
from bs4 import BeautifulSoup
from datetime import datetime, timezone

app = Flask(__name__, static_folder=".")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

SOURCES = [
    "https://arsiv.mackolik.com/Iddaa-Programi",
    "https://www.mackolik.com/iddaa",
]

_cache = {"time": 0, "matches": []}
CACHE_SECONDS = 60


def clean(text):
    return " ".join(str(text or "").split()).strip()


def odds_from_text(text):
    return [v.replace(",", ".") for v in re.findall(r"\b\d+(?:[.,]\d{2})\b", text)]


def find_match_url(node):
    for a in node.find_all("a", href=True):
        href = a.get("href", "")
        if "/mac/" in href.lower() or "/match/" in href.lower():
            return ("https://www.mackolik.com" + href) if href.startswith("/") else href
    return None


def labelled_odds(cells):
    """Geliştirilmiş etiket ve regex tarama fonksiyonu"""
    aliases = {
        "ms1": [r"\bms\s*1\b", r"\b1\b"],
        "msx": [r"\bms\s*x\b", r"\bx\b"],
        "ms2": [r"\bms\s*2\b", r"\b2\b"],
        "cs1x": [r"1-x", r"1x"],
        "csx2": [r"x-2", r"x2"],
        "cs12": [r"1-2", r"12"],
        "over15": [r"ü(?:st)?\s*1[.,]5", r"1[.,]5\s*ü"],
        "under15": [r"a(?:lt)?\s*1[.,]5", r"1[.,]5\s*a"],
        "over25": [r"ü(?:st)?\s*2[.,]5", r"2[.,]5\s*ü"],
        "under25": [r"a(?:lt)?\s*2[.,]5", r"2[.,]5\s*a"],
        "over35": [r"ü(?:st)?\s*3[.,]5", r"3[.,]5\s*ü"],
        "under35": [r"a(?:lt)?\s*3[.,]5", r"3[.,]5\s*a"],
        "bttsYes": [r"kg\s*v", r"var", r"kg\s*var"],
        "bttsNo": [r"kg\s*y", r"yok", r"kg\s*yok"],
    }
    result = {}
    for cell in cells:
        txt = clean(cell.get_text(" ", strip=True))
        vals = odds_from_text(txt)
        if not vals:
            continue
        for key, patterns in aliases.items():
            if key in result:
                continue
            if any(re.search(p, txt, re.I) for p in patterns):
                result[key] = vals[-1]
    return result


def extract_market_values(raw):
    """Metin üzerinden genişletilmiş market çekici"""
    text = clean(raw).replace("\u00a0", " ")

    def after_heading(patterns, count, window=300):
        for pat in patterns:
            m = re.search(pat, text, re.I)
            if not m:
                continue
            tail = text[m.end():m.end()+window]
            vals = odds_from_text(tail)
            if len(vals) >= count:
                return vals[:count]
        return []

    out = {}

    v = after_heading([r"Maç\s*Sonucu(?:\s*\(MS\))?", r"\bMS\b"], 3)
    if len(v) == 3:
        out.update(ms1=v[0], msx=v[1], ms2=v[2])

    v = after_heading([r"Çifte\s*Şans", r"\bÇŞ\b"], 3)
    if len(v) == 3:
        out.update(cs1x=v[0], csx2=v[1], cs12=v[2])

    for label, key1, key2 in [
        (r"0[,.]5\s*Alt\s*/?\s*Üst", "htUnder05", "htOver05"),
        (r"1[,.]5\s*Alt\s*/?\s*Üst", "htUnder15", "htOver15"),
        (r"2[,.]5\s*Alt\s*/?\s*Üst", "under25", "over25"),
        (r"3[,.]5\s*Alt\s*/?\s*Üst", "under35", "over35"),
    ]:
        v = after_heading([label], 2, 200)
        if len(v) == 2:
            out[key1], out[key2] = v[0], v[1]

    v = after_heading([r"Karşılıklı\s*Gol", r"KG\s*Var\s*/?\s*Yok", r"\bKG\b"], 2, 200)
    if len(v) == 2:
        out["bttsYes"], out["bttsNo"] = v[0], v[1]

    v = after_heading([r"İlk\s*Yarı\s*Sonucu", r"\bİY\b"], 3, 220)
    if len(v) == 3:
        out["ht1"], out["htX"], out["ht2"] = v[0], v[1], v[2]

    v = after_heading([r"İlk\s*Yarı\s*/\s*Maç\s*Sonucu", r"İY\s*/\s*MS"], 9, 500)
    if len(v) >= 9:
        keys = ["htFt11","htFt1X","htFt12","htFtX1","htFtXX","htFtX2","htFt21","htFt2X","htFt22"]
        out.update(dict(zip(keys, v[:9])))

    return out


def parse_archive(soup):
    out = []
    current_league = ""
    league_words = [
        "Süper Lig", "TFF", "Premier Lig", "LaLiga", "Serie A", "Serie B",
        "Bundesliga", "Eredivisie", "Ligue 1", "Pro Lig", "Championship",
        "2.Lig", "1. Lig", "Liga", "Division", "Kupası"
    ]

    for tr in soup.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        texts = [clean(c.get_text(" ", strip=True)) for c in cells]
        if not texts:
            continue
        row_text = " | ".join(texts)

        if any(w.lower() in row_text.lower() for w in league_words):
            if not re.search(r"\b\d{1,2}:\d{2}\b", row_text) and " - " not in row_text:
                current_league = clean(row_text)
                continue

        team_idx = next((i for i, c in enumerate(texts) if " - " in c and len(c) < 120), None)
        if team_idx is None:
            continue

        teams = texts[team_idx].split(" - ", 1)
        if len(teams) != 2:
            continue
        home, away = teams[0].strip(), teams[1].strip()
        if not home or not away:
            continue

        row_cells = cells[team_idx + 1:]
        row_after = " ".join(texts[team_idx + 1:])
        labelled = labelled_odds(row_cells)
        vals = odds_from_text(row_after)

        ms1 = labelled.get("ms1") or (vals[0] if len(vals) > 0 else None)
        msx = labelled.get("msx") or (vals[1] if len(vals) > 1 else None)
        ms2 = labelled.get("ms2") or (vals[2] if len(vals) > 2 else None)

        # Esnek Alt/Üst ve KG yedeklemeleri (fallback)
        over25 = labelled.get("over25")
        under25 = labelled.get("under25")
        if not (over25 and under25) and len(vals) >= 5:
            pair = vals[3:5] if len(vals) >= 5 else vals[-2:]
            if len(pair) == 2:
                try:
                    a, b = float(pair[0]), float(pair[1])
                    over25, under25 = (pair if a >= b else [pair[1], pair[0]])
                except ValueError:
                    pass

        bttsYes = labelled.get("bttsYes") or (vals[5] if len(vals) > 5 else None)
        bttsNo = labelled.get("bttsNo") or (vals[6] if len(vals) > 6 else None)

        out.append({
            "url": find_match_url(tr),
            "home": home,
            "away": away,
            "league": current_league,
            "time": (re.search(r"\b\d{1,2}:\d{2}\b", row_text).group(0)
                     if re.search(r"\b\d{1,2}:\d{2}\b", row_text) else ""),
            "ms1": ms1,
            "msx": msx,
            "ms2": ms2,
            "markets": {
                "cs1x": labelled.get("cs1x"),
                "csx2": labelled.get("csx2"),
                "cs12": labelled.get("cs12"),
                "over15": labelled.get("over15"),
                "under15": labelled.get("under15"),
                "over25": over25,
                "under25": under25,
                "over35": labelled.get("over35"),
                "under35": labelled.get("under35"),
                "bttsYes": bttsYes,
                "bttsNo": bttsNo,
            },
        })
    return out


def parse_main_page(soup):
    out = []
    for a in soup.find_all("a", href=True):
        txt = clean(a.get_text(" ", strip=True))
        href = a.get("href", "")
        if "/mac/" not in href or " - " not in txt:
            continue
        home, away = [x.strip() for x in txt.split(" - ", 1)]
        if not home or not away or len(home) > 80 or len(away) > 80:
            continue
        parent = a.parent
        block = parent if parent else a
        cells = block.find_all(["td", "th"]) or [block]
        labelled = labelled_odds(cells)
        vals = odds_from_text(clean(block.get_text(" ", strip=True)))
        ms1 = labelled.get("ms1") or (vals[0] if len(vals) > 0 else None)
        msx = labelled.get("msx") or (vals[1] if len(vals) > 1 else None)
        ms2 = labelled.get("ms2") or (vals[2] if len(vals) > 2 else None)
        over25 = labelled.get("over25")
        under25 = labelled.get("under25")
        if not (over25 and under25) and len(vals) >= 5:
            pair = vals[-2:]
            try:
                aa, bb = float(pair[0]), float(pair[1])
                over25, under25 = (pair if aa >= bb else [pair[1], pair[0]])
            except ValueError:
                pass
        out.append({
            "url": ("https://www.mackolik.com" + href) if href.startswith("/") else href,
            "home": home, "away": away, "league": "", "time": "",
            "ms1": ms1, "msx": msx, "ms2": ms2,
            "markets": {
                "cs1x": labelled.get("cs1x"), "csx2": labelled.get("csx2"), "cs12": labelled.get("cs12"),
                "over15": labelled.get("over15"), "under15": labelled.get("under15"),
                "over25": over25, "under25": under25,
                "over35": labelled.get("over35"), "under35": labelled.get("under35"),
                "bttsYes": labelled.get("bttsYes"), "bttsNo": labelled.get("bttsNo"),
            },
        })
    return out


def fetch_matches():
    last_error = None
    headers = {"User-Agent": UA, "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8"}
    for url in SOURCES:
        try:
            r = requests.get(url, headers=headers, timeout=(8, 35))
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")
            matches = parse_archive(soup) if "arsiv.mackolik.com" in url else parse_main_page(soup)
            if matches:
                clean_matches, seen = [], set()
                for x in matches:
                    key = (x["home"].lower(), x["away"].lower(), x["time"])
                    if key not in seen:
                        seen.add(key)
                        clean_matches.append(x)
                return clean_matches[:200]
        except Exception as e:
            last_error = f"{url}: {e}"
    raise RuntimeError(f"Mackolik verisi alınamadı. Son hata: {last_error}")


def parse_matches():
    now = time.time()
    if _cache["matches"] and now - _cache["time"] < CACHE_SECONDS:
        return _cache["matches"]
    matches = fetch_matches()
    _cache["matches"] = matches
    _cache["time"] = now
    return matches


@app.get("/")
def home():
    return send_from_directory(".", "index.html")


@app.get("/api/matches")
def matches():
    try:
        data = parse_matches()
        return jsonify(ok=True, matches=data, count=len(data), source="Mackolik",
                       time=datetime.now(timezone.utc).isoformat() + "Z")
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 502


@app.get("/api/match-details")
def match_details():
    url = request.args.get("url")
    if not url or url == "default":
        return jsonify(ok=False, error="Maç detay URL'si bulunamadı"), 400
    headers = {"User-Agent": UA, "Accept-Language": "tr-TR,tr;q=0.9"}
    try:
        r = requests.get(url, headers=headers, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        raw = soup.get_text(" ", strip=True)

        scores = [(int(h), int(a)) for h, a in re.findall(r"\b([0-9])\s*-\s*([0-9])\b", raw)]
        home_form, away_form = [], []
        for h, a in scores[:5]:
            home_form.append("G" if h > a else "B" if h == a else "M")
        for h, a in scores[5:10]:
            away_form.append("G" if a > h else "B" if a == h else "M")

        markets = extract_market_values(raw)
        return jsonify(ok=True, home_form=home_form, away_form=away_form, markets=markets)
    except Exception as e:
        return jsonify(ok=False, error=f"Maç detayları alınamadı: {e}"), 502


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
