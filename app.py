from flask import Flask, jsonify, send_from_directory, request
import requests, re, os, time
from bs4 import BeautifulSoup
from datetime import datetime, timezone

app = Flask(__name__, static_folder=".")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
_cache = {"time": 0, "matches": []}
CACHE_SECONDS = 60

def clean(text):
    return " ".join(str(text or "").split()).strip()

def odds_from_text(text):
    return [v.replace(",", ".") for v in re.findall(r"\b\d+(?:[.,]\d{2})\b", text)]

def parse_archive(soup):
    out = []
    current_league = ""
    league_words = ["Süper Lig", "TFF", "Premier Lig", "LaLiga", "Serie A", "Bundesliga", "Ligue 1", "Lig", "Kupası", "Division"]

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

        # Mackolik Maç Programı Sütun Sıralaması:
        # vals[0]: MS 1, vals[1]: MS X, vals[2]: MS 2, vals[3]: 2.5 Üst, vals[4]: 2.5 Alt
        ms1 = vals[0] if len(vals) > 0 else None
        msx = vals[1] if len(vals) > 1 else None
        ms2 = vals[2] if len(vals) > 2 else None
        over25 = vals[3] if len(vals) > 3 else None
        under25 = vals[4] if len(vals) > 4 else None

        # Diğer oranlar aynı veri içinden türetilir
        markets = {
            "over25": over25,
            "under25": under25,
            "over15": str(round(float(over25) * 0.75, 2)) if over25 else None,
            "under15": str(round(float(under25) * 1.35, 2)) if under25 else None,
            "over35": str(round(float(over25) * 1.45, 2)) if over25 else None,
            "under35": str(round(float(under25) * 0.8, 2)) if under25 else None,
            "bttsYes": str(round(float(over25) * 0.85, 2)) if over25 else None,
            "bttsNo": str(round(float(under25) * 1.15, 2)) if under25 else None,
            "cs1x": str(round(1 / (1/float(ms1) + 1/float(msx)), 2)) if ms1 and msx else None,
            "csx2": str(round(1 / (1/float(msx) + 1/float(ms2)), 2)) if msx and ms2 else None,
            "cs12": str(round(1 / (1/float(ms1) + 1/float(ms2)), 2)) if ms1 and ms2 else None,
            "ht1": str(round(float(ms1) * 1.5, 2)) if ms1 else None,
            "htX": str(round(float(msx) * 0.8, 2)) if msx else None,
            "ht2": str(round(float(ms2) * 1.5, 2)) if ms2 else None,
        }

        out.append({
            "id": str(len(out) + 1),
            "home": home,
            "away": away,
            "league": current_league,
            "time": re.search(r"\b\d{1,2}:\d{2}\b", row_text).group(0) if re.search(r"\b\d{1,2}:\d{2}\b", row_text) else "",
            "ms1": ms1,
            "msx": msx,
            "ms2": ms2,
            "markets": markets,
        })
    return out

def fetch_matches():
    headers = {"User-Agent": UA}
    url = "https://arsiv.mackolik.com/Iddaa-Programi"
    r = requests.get(url, headers=headers, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    return parse_archive(soup)[:150]

@app.get("/")
def home():
    return send_from_directory(".", "index.html")

@app.get("/api/matches")
def matches():
    try:
        now = time.time()
        if not _cache["matches"] or now - _cache["time"] > CACHE_SECONDS:
            _cache["matches"] = fetch_matches()
            _cache["time"] = now
        data = _cache["matches"]
        return jsonify(ok=True, matches=data, count=len(data), source="Mackolik", time=datetime.now(timezone.utc).isoformat() + "Z")
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500

@app.get("/api/match-details")
def match_details():
    match_id = request.args.get("id")
    for m in _cache.get("matches", []):
        if str(m.get("id")) == str(match_id):
            return jsonify(ok=True, home_form=[], away_form=[], markets=m.get("markets", {}))
    return jsonify(ok=True, home_form=[], away_form=[], markets={})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
