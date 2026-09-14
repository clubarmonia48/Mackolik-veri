from flask import Flask, jsonify, send_from_directory, request
import requests, os, time
from datetime import datetime, timezone

app = Flask(__name__, static_folder=".")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
_cache = {"time": 0, "matches": []}
CACHE_SECONDS = 60

def fetch_real_bulletin():
    url = "https://bulletin.nesine.com/api/bulletin/getevents"
    headers = {"User-Agent": UA, "Accept": "application/json"}
    
    r = requests.get(url, headers=headers, timeout=12)
    r.raise_for_status()
    data = r.json()

    matches = []
    events = data.get("eventIdList", []) or data.get("events", []) or []
    
    for ev in events:
        home = ev.get("homeTeamName") or ev.get("hn") or ""
        away = ev.get("awayTeamName") or ev.get("an") or ""
        if not home or not away:
            continue
            
        m_id = str(ev.get("eventId") or ev.get("id") or "")
        league = ev.get("leagueName") or ev.get("cn") or ""
        m_time = ev.get("eventDate") or ev.get("t") or ""
        
        markets = {}
        for m in ev.get("markets", []) or ev.get("m", []):
            m_name = str(m.get("marketName") or m.get("name") or m.get("n") or "").lower()
            m_type = str(m.get("marketType") or m.get("t") or "")
            o_list = m.get("odds", []) or m.get("o", [])
            
            # Maç Sonucu (1-X-2)
            if m_type in ["1", "MS"] or "maç sonucu" in m_name:
                for o in o_list:
                    n = str(o.get("name") or o.get("n") or "").upper()
                    v = str(o.get("odd") or o.get("o") or "")
                    if n == "1": markets["ms1"] = v
                    elif n in ["X", "0"]: markets["msx"] = v
                    elif n == "2": markets["ms2"] = v
                    
            # 2.5 Alt / Üst (Nesine Sıralaması: 0 -> Üst, 1 -> Alt)
            elif "2.5" in m_name:
                for idx, o in enumerate(o_list):
                    n = str(o.get("name") or o.get("n") or "").lower()
                    v = str(o.get("odd") or o.get("o") or "")
                    if "alt" in n or (not n and idx == 1):
                        markets["under25"] = v
                    elif "üst" in n or "ust" in n or (not n and idx == 0):
                        markets["over25"] = v

            # 1.5 Alt / Üst
            elif "1.5" in m_name:
                for idx, o in enumerate(o_list):
                    n = str(o.get("name") or o.get("n") or "").lower()
                    v = str(o.get("odd") or o.get("o") or "")
                    if "alt" in n or (not n and idx == 1):
                        markets["under15"] = v
                    elif "üst" in n or "ust" in n or (not n and idx == 0):
                        markets["over15"] = v

            # 3.5 Alt / Üst
            elif "3.5" in m_name:
                for idx, o in enumerate(o_list):
                    n = str(o.get("name") or o.get("n") or "").lower()
                    v = str(o.get("odd") or o.get("o") or "")
                    if "alt" in n or (not n and idx == 1):
                        markets["under35"] = v
                    elif "üst" in n or "ust" in n or (not n and idx == 0):
                        markets["over35"] = v

            # Karşılıklı Gol (0 -> Var, 1 -> Yok)
            elif "kg" in m_name or "karşılıklı" in m_name:
                for idx, o in enumerate(o_list):
                    n = str(o.get("name") or o.get("n") or "").lower()
                    v = str(o.get("odd") or o.get("o") or "")
                    if "var" in n or (not n and idx == 0):
                        markets["bttsYes"] = v
                    elif "yok" in n or (not n and idx == 1):
                        markets["bttsNo"] = v

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
            _cache["matches"] = fetch_real_bulletin()
            _cache["time"] = now
        data = _cache["matches"]
        return jsonify(ok=True, matches=data, count=len(data), source="NesineAPI", time=datetime.now(timezone.utc).isoformat() + "Z")
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
    
