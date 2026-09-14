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


def extract_market_values(raw):
    """Maç detay sayfasındaki tüm market alanlarını hassas şekilde ayrıştırır."""
    text = clean(raw).replace("\u00a0", " ")
    out = {}

    def get_numbers_after(pattern, count=2, window=250):
        m = re.search(pattern, text, re.I)
        if not m:
            return []
        tail = text[m.end():m.end()+window]
        return odds_from_text(tail)[:count]

    # Maç Sonucu
    v = get_numbers_after(r"Maç\s*Sonucu", 3)
    if len(v) == 3:
        out.update(ms1=v[0], msx=v[1], ms2=v[2])

    # Çifte Şans
    v = get_numbers_after(r"Çifte\s*Şans", 3)
    if len(v) == 3:
        out.update(cs1x=v[0], csx2=v[1], cs12=v[2])

    # Alt / Üst Marketleri (Maçkolik'te varsayılan düzen: Alt, Üst)
    for pat, k_under, k_over in [
        (r"0[,.]5\s*Alt\s*/?\s*Üst", "htUnder05", "htOver05"),
        (r"1[,.]5\s*Alt\s*/?\s*Üst", "under15", "over15"),
        (r"2[,.]5\s*Alt\s*/?\s*Üst", "under25", "over25"),
        (r"3[,.]5\s*Alt\s*/?\s*Üst", "under35", "over35"),
    ]:
        v = get_numbers_after(pat, 2)
        if len(v) == 2:
            out[k_under], out[k_over] = v[0], v[1]

    # İlk Yarı 1.5 Alt/Üst Özel Tespiti
    v = get_numbers_after(r"İlk\s*Yarı\s*1[,.]5\s*Alt\s*/?\s*Üst", 2)
    if len(v) == 2:
        out["htUnder15"], out["htOver15"] = v[0], v[1]

    # Karşılıklı Gol
    v = get_numbers_after(r"Karşılıklı\s*Gol", 2)
    if len(v) == 2:
        out["bttsYes"], out["bttsNo"] = v[0], v[1]

    # İlk Yarı Sonucu
    v = get_numbers_after(r"İlk\s*Yarı\s*Sonucu", 3)
    if len(v) == 3:
        out["ht1"], out["htX"], out["ht2"] = v[0], v[1], v[2]

    # İY / MS
    v = get_numbers_after(r"İlk\s*Yarı\s*/\s*Maç\s*Sonucu", 9, 450)
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

        row_after = " ".join(texts[team_idx + 1:])
        vals = odds_from_text(row_after)

        ms1 = vals[0] if len(vals) > 0 else None
        msx = vals[1] if len(vals) > 1 else None
        ms2 = vals[2] if len(vals) > 2 else None

        over25, under25 = None, None
        if len(vals) >= 5:
            pair = vals[3:5]
            try:
                a, b = float(pair[0]), float(pair[1])
                over25, under25 = (pair[0], pair[1]) if a >= b else (pair[1], pair[0])
            except ValueError:
                pass

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
                "over25": over25,
                "under25": under25,
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
        block = a.parent or a
        vals = odds_from_text(clean(block.get_text(" ", strip=True)))
        
        ms1 = vals[0] if len(vals) > 0 else None
        msx = vals[1] if len(vals) > 1 else None
        ms2 = vals[2] if len(vals) > 2 else None
        
        over25, under25 = None, None
        if len(vals) >= 5:
            pair = vals[-2:]
            try:
                a, b = float(pair[0]), float(pair[1])
                over25, under25 = (pair[0], pair[1]) if a >= b else (pair[1], pair[0])
            except ValueError:
                pass

        out.append({
            "url": ("https://www.mackolik.com" + href) if href.startswith("/") else href,
            "home": home, "away": away, "league": "", "time": "",
            "ms1": ms1, "msx": msx, "ms2": ms2,
            "markets": {"over25": over25, "under25": under25},
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
