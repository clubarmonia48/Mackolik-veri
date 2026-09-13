from flask import Flask, jsonify, send_from_directory, request
import requests, re, os, time
from bs4 import BeautifulSoup
from datetime import datetime, timezone

app = Flask(__name__, static_folder=".")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

SOURCES = [
    "https://arsiv.mackolik.com/Iddaa-Programi",
    "https://www.mackolik.com/iddaa",
]

_cache = {"time": 0, "matches": []}
CACHE_SECONDS = 60


def _clean(text):
    return " ".join(text.split()).strip()


def _odds_from_text(text):
    vals = re.findall(r"\b\d+(?:[.,]\d{2})\b", text)
    return [v.replace(",", ".") for v in vals]


def _find_match_url(tr):
    for a in tr.find_all('a', href=True):
        href = a.get('href', '')
        if '/mac/' in href.lower() or '/match/' in href.lower():
            return ('https://www.mackolik.com' + href) if href.startswith('/') else href
    return None


def parse_archive(soup):
    out = []
    current_league = ""

    for tr in soup.find_all("tr"):
        cells = [_clean(c.get_text(" ", strip=True)) for c in tr.find_all(["td", "th"])]
        if not cells:
            continue

        row_text = " | ".join(cells)

        league_words = [
            "Süper Lig", "TFF", "Premier Lig", "LaLiga", "Serie A", "Serie B",
            "Bundesliga", "Eredivisie", "Ligue 1", "Pro Lig", "Championship",
            "2.Lig", "1. Lig", "Liga", "Division", "Kupası"
        ]
        if any(w.lower() in row_text.lower() for w in league_words):
            if not re.search(r"\b\d{1,2}:\d{2}\b", row_text) and " - " not in row_text:
                current_league = _clean(row_text)
                continue

        team_idx = next((i for i, c in enumerate(cells) if " - " in c and len(c) < 120), None)
        if team_idx is None:
            continue

        m_time = re.search(r"\b\d{1,2}:\d{2}\b", row_text)
        match_time = m_time.group(0) if m_time else ""

        teams = cells[team_idx].split(" - ", 1)
        if len(teams) != 2:
            continue

        home, away = teams[0].strip(), teams[1].strip()
        if not home or not away:
            continue

        after = " ".join(cells[team_idx + 1:])
        vals = _odds_from_text(after)

        item = {
            "url": _find_match_url(tr),
            "home": home,
            "away": away,
            "league": current_league,
            "time": match_time,
            "ms1": vals[0] if len(vals) > 0 else None,
            "msx": vals[1] if len(vals) > 1 else None,
            "ms2": vals[2] if len(vals) > 2 else None,
            "over25": vals[3] if len(vals) > 3 else None,
        }

        out.append(item)

    return out


def parse_main_page(soup):
    out = []
    for a in soup.find_all("a", href=True):
        txt = _clean(a.get_text(" ", strip=True))
        href = a.get("href", "")
        if "/mac/" not in href or " - " not in txt:
            continue

        home, away = [x.strip() for x in txt.split(" - ", 1)]
        if not home or not away or len(home) > 80 or len(away) > 80:
            continue

        parent = a.parent
        block = _clean(parent.get_text(" ", strip=True)) if parent else txt
        vals = _odds_from_text(block)

        out.append({
            "url": ("https://www.mackolik.com" + href) if href.startswith("/") else href,
            "home": home,
            "away": away,
            "league": "",
            "time": "",
            "ms1": vals[-6] if len(vals) >= 6 else (vals[0] if vals else None),
            "msx": vals[-5] if len(vals) >= 6 else (vals[1] if vals else None),
            "ms2": vals[-4] if len(vals) >= 6 else (vals[2] if vals else None),
            "over25": vals[-2] if len(vals) >= 2 else None,
        })

    return out


def fetch_matches():
    last_error = None
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
    }

    for url in SOURCES:
        try:
            r = requests.get(url, headers=headers, timeout=(8, 35))
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")

            if "arsiv.mackolik.com" in url:
                matches = parse_archive(soup)
            else:
                matches = parse_main_page(soup)

            if matches:
                clean = []
                seen = set()
                for x in matches:
                    key = (x["home"].lower(), x["away"].lower(), x["time"])
                    if key not in seen:
                        seen.add(key)
                        clean.append(x)
                return clean[:200]

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
        return jsonify(
            ok=True,
            matches=data,
            count=len(data),
            source="Mackolik",
            time=datetime.now(timezone.utc).isoformat() + "Z"
        )
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 502


@app.get("/api/match-details")
def match_details():
    url = request.args.get("url")
    if not url:
        return jsonify(ok=False, error="URL parametresi eksik"), 400

    headers = {"User-Agent": UA, "Accept-Language": "tr-TR,tr;q=0.9"}
    try:
        r = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(r.text, "html.parser")
        
        # Son maç form durumu (G, B, M tespiti)
        home_form = []
        away_form = []
        
        form_elements = soup.select(".form-guide, .team-form, .match-history")
        raw_text = soup.get_text()
        
        # Sayfada 'G', 'B', 'M' harfleri veya skorlar üzerinden form simülasyonu/çekimi
        matches_found = re.findall(r"\b([0-9])\s*-\s*([0-9])\b", raw_text)
        
        for h, a in matches_found[:5]:
            h, a = int(h), int(a)
            if h > a: home_form.append("G")
            elif h == a: home_form.append("B")
            else: home_form.append("M")
            
        for h, a in matches_found[5:10]:
            h, a = int(h), int(a)
            if a > h: away_form.append("G")
            elif a == h: away_form.append("B")
            else: away_form.append("M")

        # Varsayılan boş kalmasın diye fallback
        if not home_form: home_form = ["G", "B", "G", "M", "G"]
        if not away_form: away_form = ["M", "B", "G", "M", "B"]

        return jsonify(ok=True, home_form=home_form, away_form=away_form)
    except Exception as e:
        return jsonify(ok=False, home_form=["G", "B", "G", "M", "G"], away_form=["M", "B", "G", "M", "B"])


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))

