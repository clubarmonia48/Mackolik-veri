from flask import Flask, jsonify, send_from_directory, request
import requests, re, os, time
from bs4 import BeautifulSoup
from datetime import datetime, timezone

app = Flask(__name__, static_folder=".")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

_cache = {"time": 0, "matches": []}
CACHE_SECONDS = 60


def clean(text):
    return " ".join(str(text or "").split()).strip()


def odds_from_text(text):
    return [v.replace(",", ".") for v in re.findall(r"\b\d+(?:[.,]\d{2})\b", text)]


def find_match_id_or_url(node):
    for a in node.find_all("a", href=True):
        href = a.get("href", "")
        # Maç ID'sini URL içerisinden yakala
        match_id = re.search(r"/(?:mac|match)/[^/]+(?:/)?([a-zA-Z0-9]+)", href)
        if match_id:
            m_id = match_id.group(1)
            full_url = ("https://www.mackolik.com" + href) if href.startswith("/") else href
            return m_id, full_url
    return None, None


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

        m_id, m_url = find_match_id_or_url(tr)

        row_after = " ".join(texts[team_idx + 1:])
        vals = odds_from_text(row_after)

        ms1 = vals[0] if len(vals) > 0 else None
        msx = vals[1] if len(vals) > 1 else None
        ms2 = vals[2] if len(vals) > 2 else None

        # Mackolik bülten yapısında varsayılan 2.5 Alt/Üst sırası: 1. Üst, 2. Alt
        over25 = vals[3] if len(vals) > 3 else None
        under25 = vals[4] if len(vals) > 4 else None

        out.append({
            "id": m_id,
            "url": m_url,
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


def fetch_matches():
    headers = {"User-Agent": UA, "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8"}
    url = "https://arsiv.mackolik.com/Iddaa-Programi"
    r = requests.get(url, headers=headers, timeout=(8, 35))
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    matches = parse_archive(soup)
    
    clean_matches, seen = [], set()
    for x in matches:
        key = (x["home"].lower(), x["away"].lower(), x["time"])
        if key not in seen:
            seen.add(key)
            clean_matches.append(x)
    return clean_matches[:200]


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
    match_id = request.args.get("id")
    url = request.args.get("url")
    
    headers = {"User-Agent": UA, "Accept-Language": "tr-TR,tr;q=0.9"}
    markets = {}
    home_form, away_form = [], []

    # 1. Maçkolik widget/API servisinden canlı oran verisini çekme
    if match_id:
        try:
            api_url = f"https://widget.mackolik.com/api/iddaa/match/{match_id}/odds"
            r_api = requests.get(api_url, headers=headers, timeout=10)
            if r_api.status_code == 200:
                data = r_api.json()
                # API içerisindeki market verilerini maple
                for m in data.get("data", {}).get("markets", []):
                    m_name = m.get("name", "").lower()
                    odds = m.get("odds", [])
                    
                    if "maç sonucu" in m_name and len(odds) >= 3:
                        markets.update(ms1=str(odds[0]["value"]), msx=str(odds[1]["value"]), ms2=str(odds[2]["value"]))
                    elif "çifte şans" in m_name and len(odds) >= 3:
                        markets.update(cs1x=str(odds[0]["value"]), cs12=str(odds[1]["value"]), csx2=str(odds[2]["value"]))
                    elif "karşılıklı gol" in m_name and len(odds) >= 2:
                        markets.update(bttsYes=str(odds[0]["value"]), bttsNo=str(odds[1]["value"]))
                    elif "2.5 alt/üst" in m_name and len(odds) >= 2:
                        markets.update(over25=str(odds[0]["value"]), under25=str(odds[1]["value"]))
                    elif "1.5 alt/üst" in m_name and len(odds) >= 2:
                        markets.update(over15=str(odds[0]["value"]), under15=str(odds[1]["value"]))
                    elif "3.5 alt/üst" in m_name and len(odds) >= 2:
                        markets.update(over35=str(odds[0]["value"]), under35=str(odds[1]["value"]))
                    elif "ilk yarı sonucu" in m_name and len(odds) >= 3:
                        markets.update(ht1=str(odds[0]["value"]), htX=str(odds[1]["value"]), ht2=str(odds[2]["value"]))
        except Exception:
            pass

    # 2. Form durumunu web sayfasından ayrıştırma
    if url and url != "default":
        try:
            r = requests.get(url, headers=headers, timeout=10)
            if r.status_code == 200:
                soup = BeautifulSoup(r.text, "html.parser")
                raw = soup.get_text(" ", strip=True)
                scores = [(int(h), int(a)) for h, a in re.findall(r"\b([0-9])\s*-\s*([0-9])\b", raw)]
                for h, a in scores[:5]:
                    home_form.append("G" if h > a else "B" if h == a else "M")
                for h, a in scores[5:10]:
                    away_form.append("G" if a > h else "B" if a == h else "M")
        except Exception:
            pass

    return jsonify(ok=True, home_form=home_form, away_form=away_form, markets=markets)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
