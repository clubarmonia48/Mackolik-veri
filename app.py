from flask import Flask, jsonify, send_from_directory, request
import requests, os, time
from datetime import datetime, timezone

app = Flask(__name__, static_folder=".")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
_cache = {"time": 0, "matches": []}
CACHE_SECONDS = 60

def fetch_iddaa_bulletin():
    # Güncel ve çalışan iddaa API adresi
    url = "https://sports.iddaa.com/api/bulletin/events?type=1"
    headers = {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://www.iddaa.com",
        "Referer": "https://www.iddaa.com/"
    }
    
    r = requests.get(url, headers=headers, timeout=15)
    r.raise_for_status()
    data = r.json()
    
    matches = []
    events = data.get("data", []) if isinstance(data.get("data"), list) else data.get("events", [])
    
    for ev in events:
        home = ev.get("homeTeamName") or ev.get("hn") or ""
        away = ev.get("awayTeamName") or ev.get("an") or ""
        if not home or not away:
            continue
            
        m_id = str(ev.get("id") or ev.get("eventId") or "")
        league = ev.get("categoryName") or ev.get("cn") or ""
        m_time = ev.get("eventDate") or ev.get("t") or ""
        
        markets = {}
        
        # Bahis marketlerini işle
        for m in ev.get("markets", []) or ev.get("m", []):
            m_name = (m.get("name") or m.get("n") or "").lower()
            o_list = m.get("odds", []) or m.get("o", [])
            
            # Maç Sonucu (1-X-2)
            if "maç sonucu" in m_name or m.get("code") == "1":
                for o in o_list:
                    n = str(o.get("name") or o.get("n") or "").strip().upper()
                    v = str(o.get("odd") or o.get("o") or "")
                    if n == "1": markets["ms1"] = v
                    elif n == "X": markets["msx"] = v
                    elif n == "2": markets["ms2"] = v

            # 2.5 Alt / Üst (1: Alt, 2: Üst)
            elif "2.5" in m_name or "alt/üst 2.5" in m_name:
                for o in o_list:
                    n = str(o.get("name") or o.get("n") or "").strip().lower()
                    v = str(o.get("odd") or o.get("o") or "")
                    if "alt" in n or n == "1": markets["under25"] = v
                    elif "üst" in n or "ust" in n or n == "2": markets["over25"] = v

            # 1.5 Alt / Üst
            elif "1.5" in m_name:
                for o in o_list:
                    n = str(o.get("name") or o.get("n") or "").strip().lower()
                    v = str(o.get("odd") or o.get("o") or "")
                    if "alt" in n or n == "1": markets["under15"] = v
                    elif "üst" in n or "ust" in n or n == "2": markets["over15"] = v

            # 3.5 Alt / Üst
            elif "3.5" in m_name:
                for o in o_list:
                    n = str(o.get("name") or o.get("n") or "").strip().lower()
                    v = str(o.get("odd") or o.get("o") or "")
                    if "alt" in n or n == "1": markets["under35"] = v
                    elif "üst" in n or "ust" in n or n == "2": markets["over35"] = v

            # Karşılıklı Gol (1: Var, 2: Yok)
            elif "karşılıklı" in m_name or "kg" in m_name:
                for o in o_list:
                    n = str(o.get("name") or o.get("n") or "").strip().lower()
                    v = str(o.get("odd") or o.get("o") or "")
                    if "var" in n or n == "1": markets["bttsYes"] = v
                    elif "yok" in n or n == "2": markets["bttsNo"] = v

            # Çifte Şans
            elif "çifte şans" in m_name:
                for o in o_list:
                    n = str(o.get("name") or o.get("n") or "").strip().upper()
                    v = str(o.get("odd") or o.get("o") or "")
                    if "1-X" in n or "1X" in n: markets["cs1x"] = v
                    elif "1-2" in n or "12" in n: markets["cs12"] = v
                    elif "X-2" in n or "X2" in n: markets["csx2"] = v

            # İlk Yarı Sonucu
            elif "ilk yarı sonucu" in m_name:
                for o in o_list:
                    n = str(o.get("name") or o.get("n") or "").strip().upper()
                    v = str(o.get("odd") or o.get("o") or "")
                    if n == "1": markets["ht1"] = v
                    elif n == "X": markets["htX"] = v
                    elif n == "2": markets["ht2"] = v

        matches.append({
            "id": m_id,
            "home": home,
            "away": away,
            "league": league,
            "time": m_time,
            "ms1": markets.get("ms1"),
            "msx": markets.get("msx"),
            "ms2": markets.get("ms2"),
            "markets": markets
        })

    return matches

@app.get("/")
def home():
    return send_from_directory(".", "index.html")

@app.get("/api/matches")
def matches():
    try:
        now = time.time()
        if not _cache["matches"] or now - _cache["time"] > CACHE_SECONDS:
            _cache["matches"] = fetch_iddaa_bulletin()
            _cache["time"] = now
        data = _cache["matches"]
        return jsonify(ok=True, matches=data, count=len(data), source="iddaa.com",
                       time=datetime.now(timezone.utc).isoformat() + "Z")
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 502

@app.get("/api/match-details")
def match_details():
    match_id = request.args.get("id")
    if not match_id:
        return jsonify(ok=False, error="Maç ID eksik"), 400
    
    for m in _cache.get("matches", []):
        if str(m.get("id")) == str(match_id):
            return jsonify(ok=True, home_form=[], away_form=[], markets=m.get("markets", {}))
            
    return jsonify(ok=True, home_form=[], away_form=[], markets={})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
