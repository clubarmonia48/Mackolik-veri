from flask import Flask, jsonify, send_from_directory
import requests, re, os, time
from bs4 import BeautifulSoup
from datetime import datetime

app = Flask(__name__, static_folder=".")

UA = (
    "Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36"
)

# Mackolik'in ana /iddaa sayfası Render sunucusunda zaman aşımına uğrayabildiği
# için önce arşiv programını kullanıyoruz.
SOURCES = [
    "https://arsiv.mackolik.com/Iddaa-Programi",
    "https://www.mackolik.com/iddaa",
]

_cache = {"time": 0, "matches": []}
CACHE_SECONDS = 60


def _clean(text):
    return " ".join(text.split()).strip()


def _odds_from_text(text):
    # Takım kodları gibi 5 haneli sayıları alma; sadece 2 ondalıklı oranları al.
    vals = re.findall(r"\b\d+(?:[.,]\d{2})\b", text)
    return [v.replace(",", ".") for v in vals]


def parse_archive(soup):
    out = []
    current_league = ""

    # Arşiv sayfasındaki satırlar: saat + takım adı + kodlar + oranlar.
    for tr in soup.find_all("tr"):
        cells = [_clean(c.get_text(" ", strip=True)) for c in tr.find_all(["td", "th"])]
        if not cells:
            continue

        row_text = " | ".join(cells)

        # Lig başlıklarını yakala.
        league_words = [
            "Süper Lig", "TFF", "Premier Lig", "LaLiga", "Serie A", "Serie B",
            "Bundesliga", "Eredivisie", "Ligue 1", "Pro Lig", "Championship",
            "2.Lig", "1. Lig", "Liga", "Division", "Kupası"
        ]
        if any(w.lower() in row_text.lower() for w in league_words):
            # Takım satırı değilse lig başlığı olarak sakla.
            if not re.search(r"\b\d{1,2}:\d{2}\b", row_text) and " - " not in row_text:
                current_league = _clean(row_text)
                continue

        # Takım adını içeren hücreyi bul.
        team_idx = next((i for i, c in enumerate(cells) if " - " in c and len(c) < 120), None)
        if team_idx is None:
            continue

        # Saat.
        m_time = re.search(r"\b\d{1,2}:\d{2}\b", row_text)
        match_time = m_time.group(0) if m_time else ""

        teams = cells[team_idx].split(" - ", 1)
        if len(teams) != 2:
            continue

        home, away = teams[0].strip(), teams[1].strip()
        if not home or not away:
            continue

        # Takım hücresinden sonraki tüm oranları al.
        after = " ".join(cells[team_idx + 1:])
        vals = _odds_from_text(after)

        item = {
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
    # Eski ayrıştırıcıya göre daha toleranslı yedek yöntem.
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
            "home": home,
            "away": away,
            "league": "",
            "time": "",
            "ms1": vals[-6] if len(vals) >= 6 else (vals[0] if vals else None),
            "msx": vals[-5] if len(vals) >= 6 else (vals[1] if len(vals) > 1 else None),
            "ms2": vals[-4] if len(vals) >= 6 else (vals[2] if len(vals) > 2 else None),
            "over25": vals[-2] if len(vals) >= 2 else None,
        })

    return out


def fetch_matches():
    last_error = None

    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
        "Connection": "keep-alive",
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
                # Aynı maçı tekrar ekleme.
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

    raise RuntimeError(
        "Mackolik verisi alınamadı. Kaynaklara erişim başarısız oldu. "
        f"Son hata: {last_error}"
    )


def parse_matches():
    now = time.time()

    # 60 saniyelik önbellek: her butona basıldığında Mackolik'e tekrar
    # istek atılmasını ve Render'ın gereksiz yere beklemesini önler.
    if _cache["matches"] and now - _cache["time"] < CACHE_SECONDS:
        return _cache["matches"]

    matches = fetch_matches()
    _cache["matches"] = matches
    _cache["time"] = now
    return matches


@app.get("/")
def home():
    return send_from_directory(".", "index.html")


@app.get("/api/health")
def health():
    return jsonify(
        ok=True,
        time=datetime.utcnow().isoformat() + "Z"
    )


@app.get("/api/matches")
def matches():
    try:
        data = parse_matches()
        return jsonify(
            ok=True,
            matches=data,
            count=len(data),
            source="Mackolik",
            time=datetime.utcnow().isoformat() + "Z"
        )
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 502


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "10000"))
    )
