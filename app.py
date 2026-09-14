from flask import Flask, jsonify, send_from_directory, request
import requests, os, time
from datetime import datetime, timezone

app = Flask(__name__, static_folder=".")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
_cache = {"time": 0, "matches": []}
CACHE_SECONDS = 60

def fetch_iddaa_bulletin():
    url = "https://m.iddaa.com/api/v1/bulletin"
    headers = {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://www.iddaa.com",
        "Referer": "https://www.iddaa.com/"
    }
    r = requests.get(url, headers=headers, timeout=12)
    r.raise_for_status()
    data = r.json()
    
    matches = []
    events = data.get("data", {}).get("events", []) or data.get("events", [])
    
    for ev in events:
        home = ev.get("hn") or ev.get("homeTeamName") or ""
        away = ev.get("an") or ev.get("awayTeamName") or ""
        if not home or not away:
            continue
            
        m_id = str(ev.get("id") or ev.get("i") or "")
        league = ev.get("cn") or ev.get("categoryName") or ""
        m_time = ev.get("t") or ev.get("time") or ""
        
        markets = {}
        
        for m in ev.get("m", []) or ev.get("markets", []):
            m_code = str(m.get("c") or m.get("code") or "")
            m_name = (m.get("n") or m.get("name") or "").lower()
            o_list = m.get("o", []) or m.get("odds", [])
            
            # Maç Sonucu (1-X-2)
            if m_code in ["1", "101"] or "maç sonucu" in m_name:
                for o in o_list:
                    n = str(o.get("n", "")).strip().upper()
                    v = str(o.get("o", ""))
                    if n == "1": markets["ms1"] = v
                    elif n == "X": markets["msx"] = v
                    elif n == "2": markets["ms2"] = v
                    
            # 2.5 Alt / Üst (iddaa standartlarında: 1/Alt, 2/Üst veya açık metin)
            elif "2.5" in m_name or m_code == "5":
                for o in o_list:
                    n = str(o.get("n", "")).strip().lower()
                    v = str(o.get("o", ""))
                    if "alt" in n or n == "1" or n == "a":
                        markets["under25"] = v
                    elif "üst" in n or "ust" in n or n == "2" or n == "u":
                        markets["over25"] = v
                    
            # 1.5 Alt / Üst
            elif "1.5" in m_name:
                for o in o_list:
                    n = str(o.get("n", "")).strip().lower()
                    v = str(o.get("o", ""))
                    if "alt" in n or n == "1" or n == "a":
                        markets["under15"] = v
                    elif "üst" in n or "ust" in n or n == "2" or n == "u":
                        markets["over15"] = v

            # 3.5 Alt / Üst
            elif "3.5" in m_name:
                for o in o_list:
                    n = str(o.get("n", "")).strip().lower()
                    v = str(o.get("o", ""))
                    if "alt" in n or n == "1" or n == "a":
                        markets["under35"] = v
                    elif "üst" in n or "ust" in n or n == "2" or n == "u":
                        markets["over35"] = v

            # Karşılıklı Gol (1: Var, 2: Yok veya Var/Yok)
            elif "karşılıklı" in m_name or "kg" in m_name:
                for o in o_list:
                    n = str(o.get("n", "")).strip().lower()
                    v = str(o.get("o", ""))
                    if "var" in n or n == "1" or n == "v":
                        markets["bttsYes"] = v
                    elif "yok" in n or n == "2" or n == "y":
                        markets["bttsNo"] = v

            # Çifte Şans
            elif "çifte şans" in m_name or m_code == "2":
                for o in o_list:
                    n = str(o.get("n", "")).strip().upper()
                    v = str(o.get("o", ""))
                    if "1-X" in n or "1X" in n: markets["cs1x"] = v
                    elif "1-2" in n or "12" in n: markets["cs12"] = v
                    elif "X-2" in n or "X2" in n: markets["csx2"] = v

            # İlk Yarı Sonucu
            elif "ilk yarı sonucu" in m_name or m_code == "102":
                for o in o_list:
                    n = str(o.get("n", "")).strip().upper()
                    v = str(o.get("o", ""))
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
