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
    """Try to read an odds value from the same HTML cell as its market label."""
    aliases = {
        "ms1": [r"(?:maç\s*sonucu\s*)?ms\s*1\b"],
        "msx": [r"(?:maç\s*sonucu\s*)?ms\s*x\b"],
        "ms2": [r"(?:maç\s*sonucu\s*)?ms\s*2\b"],
        "over25": [r"üst\s*2[.,]5", r"over\s*2[.,]5"],
        "under25": [r"alt\s*2[.,]5", r"under\s*2[.,]5"],
        "over15": [r"üst\s*1[.,]5", r"over\s*1[.,]5"],
        "under15": [r"alt\s*1[.,]5", r"under\s*1[.,]5"],
        "over35": [r"üst\s*3[.,]5", r"over\s*3[.,]5"],
        "under35": [r"alt\s*3[.,]5", r"under\s*3[.,]5"],
        "bttsYes": [r"kg\s*var", r"karşılıklı\s*gol\s*var"],
        "bttsNo": [r"kg\s*yok", r"karşılıklı\s*gol\s*yok"],
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

        # Only use positional fallback for 1X2. For 2.5, keep both sides when
        # possible and never manufacture unrelated 1.5/3.5/KG/HT odds.
        ms1 = labelled.get("ms1")
        msx = labelled.get("msx")
        ms2 = labelled.get("ms2")
        if not (ms1 and msx and ms2) and len(vals) >= 3:
            ms1, msx, ms2 = vals[:3]

        over25 = labelled.get("over25")
        under25 = labelled.get("under25")
        if not (over25 and under25):
            # In the current archive layout the 2.5 pair is commonly the last
            # two values. We use the lower price as Over and higher as Under,
            # instead of assigning one value blindly to the wrong side.
            pair = vals[3:5] if len(vals) >= 5 else vals[-2:] if len(vals) >= 5 else []
            if len(pair) == 2:
                a, b = map(float, pair)
                # 2.5 market: higher odd is Over 2.5, lower odd is Under 2.5.
                over25, under25 = (pair if a >= b else [pair[1], pair[0]])

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
                "over15": labelled.get("over15"),
                "under15": labelled.get("under15"),
                "over25": over25,
                "under25": under25,
                "over35": labelled.get("over35"),
                "under35": labelled.get("under35"),
                "bttsYes": labelled.get("bttsYes"),
                "bttsNo": labelled.get("bttsNo"),
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
            aa, bb = map(float, pair)
            # 2.5 market: higher odd is Over 2.5, lower odd is Under 2.5.
            over25, under25 = (pair if aa >= bb else [pair[1], pair[0]])
        out.append({
            "url": ("https://www.mackolik.com" + href) if href.startswith("/") else href,
            "home": home, "away": away, "league": "", "time": "",
            "ms1": ms1, "msx": msx, "ms2": ms2,
            "markets": {
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
                        seen.add(key); clean_matches.append(x)
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
        r = requests.get(url, headers=headers, timeout=10)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        raw = soup.get_text(" ", strip=True)
        scores = [(int(h), int(a)) for h, a in re.findall(r"\b([0-9])\s*-\s*([0-9])\b", raw)]
        home_form, away_form = [], []
        for h, a in scores[:5]: home_form.append("G" if h > a else "B" if h == a else "M")
        for h, a in scores[5:10]: away_form.append("G" if a > h else "B" if a == h else "M")
        return jsonify(ok=True, home_form=home_form, away_form=away_form)
    except Exception as e:
        return jsonify(ok=False, error=f"Form verisi alınamadı: {e}"), 502


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
