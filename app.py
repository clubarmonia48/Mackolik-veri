from flask import Flask, jsonify, send_from_directory, request
import requests, re, os, time
from bs4 import BeautifulSoup
from datetime import datetime, timedelta, timezone

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


# ---------------------------------------------------------------------------
# SofaScore enrichment
# Mackolik listesi maçları ve temel oranları getiriyor. Ayrıntılı marketler,
# takımın son maçları ve H2H için seçilen maç SofaScore ile eşleştirilir.
# ---------------------------------------------------------------------------
SOFA_BASE = "https://www.sofascore.com/api/v1"
SOFA_HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json,text/plain,*/*",
    "Referer": "https://www.sofascore.com/",
}

def sofa_get(path, timeout=(8, 20)):
    r = requests.get(SOFA_BASE + path, headers=SOFA_HEADERS, timeout=timeout)
    r.raise_for_status()
    return r.json()

def norm_team_name(s):
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9çğıöşüà-ÿ]+", " ", s)
    # Yaygın isim farklarını yumuşat.
    repl = {
        "fk": "", "fc": "", "sk": "", "as": "", "spor": "",
        "kulubu": "", "kulübü": "", "belediyespor": " belediye",
    }
    for a, b in repl.items():
        s = re.sub(r"\b" + re.escape(a) + r"\b", b, s)
    return " ".join(s.split())

def team_match(a, b):
    na, nb = norm_team_name(a), norm_team_name(b)
    if na == nb:
        return True
    # Takım isimlerinden biri diğerini içeriyorsa kabul et.
    return na in nb or nb in na

def sofa_team_search(name):
    """Takım adını SofaScore'da arar; Mackolik kısa isimleriyle de dener."""
    raw=(name or "").strip()
    if not raw:
        return []
    queries=[raw]
    simplified=re.sub(r"\b(FK|FC|SK|AS|Spor|Kulübü|Kulubu)\b"," ",raw,flags=re.I)
    simplified=" ".join(simplified.split())
    if simplified and simplified.lower()!=raw.lower():
        queries.append(simplified)

    found=[]
    for q in queries:
        try:
            data=sofa_get("/search/all?q="+requests.utils.quote(q))
            for item in data.get("results",[]):
                entity=item.get("entity") or item
                kind=str(item.get("entityType") or item.get("type") or entity.get("entityType") or "").lower()
                if kind and kind!="team":
                    continue
                tid=entity.get("id")
                tname=entity.get("name","")
                if tid and tname:
                    found.append((tid,tname))
        except Exception:
            continue

    found.sort(key=lambda x:0 if team_match(raw,x[1]) else 1)
    out=[]; seen=set()
    for tid,tname in found:
        if tid not in seen:
            seen.add(tid); out.append((tid,tname))
    return out[:8]


def find_sofa_event(home, away, match_time=""):
    """SofaScore maçını takım araması, fikstür ve tarih yedeğiyle bulur."""
    now_tr=datetime.now(timezone.utc)+timedelta(hours=3)
    target_minutes=None
    if match_time:
        try:
            hh,mm=[int(x) for x in match_time.split(":")[:2]]
            target_minutes=hh*60+mm
        except Exception:
            pass

    candidates=[]
    hteams=sofa_team_search(home)
    ateams=sofa_team_search(away)
    hids={x[0] for x in hteams}
    aids={x[0] for x in ateams}

    for tid,_ in hteams[:5]:
        for endpoint in (f"/team/{tid}/events/next/0",f"/team/{tid}/events/last/0"):
            try:
                data=sofa_get(endpoint)
                for e in data.get("events",[]):
                    ht=e.get("homeTeam") or {}
                    at=e.get("awayTeam") or {}
                    if ht.get("id") in hids and at.get("id") in aids:
                        candidates.append(e)
                    elif team_match(home,ht.get("name","")) and team_match(away,at.get("name","")):
                        candidates.append(e)
            except Exception:
                continue

    for delta in (-2,-1,0,1,2):
        d=now_tr.date()+timedelta(days=delta)
        try:
            data=sofa_get(f"/sport/football/scheduled-events/{d.isoformat()}")
            for e in data.get("events",[]):
                ht=(e.get("homeTeam") or {}).get("name","")
                at=(e.get("awayTeam") or {}).get("name","")
                if team_match(home,ht) and team_match(away,at):
                    candidates.append(e)
        except Exception:
            continue

    unique=[]; seen=set()
    for e in candidates:
        eid=e.get("id")
        if eid and eid not in seen:
            seen.add(eid); unique.append(e)
    candidates=unique
    if not candidates:
        return None

    if target_minutes is not None:
        def distance(e):
            ts=e.get("startTimestamp")
            if not ts: return 999999
            dt=datetime.fromtimestamp(ts,timezone.utc)+timedelta(hours=3)
            return abs((dt.hour*60+dt.minute)-target_minutes)
        candidates.sort(key=distance)
    return candidates[0]

def decimal_choice(choice):
    candidates=[choice.get("decimalValue"),choice.get("decimalOdd"),choice.get("value")]
    odds=choice.get("odds")
    if isinstance(odds,dict):
        candidates += [odds.get("decimal"),odds.get("decimalValue"),odds.get("value")]
    elif odds is not None:
        candidates.append(odds)
    for v in candidates:
        try:
            f=float(v)
            if f>1: return f
        except Exception:
            pass
    return None

def choice_name(choice):
    return str(choice.get("name") or choice.get("label") or choice.get("choice") or "").strip()

def parse_sofa_markets(payload):
    """SofaScore market listesini frontend'in beklediği sade alanlara çevirir."""
    markets = {}
    raw_markets = payload.get("markets") or []
    if isinstance(raw_markets, dict):
        raw_markets = list(raw_markets.values())

    def put(k, v):
        if v is not None and v > 1:
            markets[k] = round(v, 3)

    for market in raw_markets:
        name = str(market.get("marketName") or market.get("name") or market.get("title") or "")
        lname = name.lower()
        choices = market.get("choices") or market.get("outcomes") or []
        if isinstance(choices, dict):
            choices = list(choices.values())

        # 1X2
        if any(x in lname for x in ("1x2", "match winner", "full time result", "maç sonucu")):
            for c in choices:
                n = choice_name(c).lower()
                v = decimal_choice(c)
                if n in ("1", "home", "home win", "ev sahibi"):
                    put("ms1", v)
                elif n in ("x", "draw", "beraberlik"):
                    put("msx", v)
                elif n in ("2", "away", "away win", "deplasman"):
                    put("ms2", v)

        # BTTS
        if "both teams" in lname or "btts" in lname or "karşılıklı gol" in lname:
            for c in choices:
                n = choice_name(c).lower()
                v = decimal_choice(c)
                if "yes" in n or "var" in n:
                    put("bttsYes", v)
                elif "no" in n or "yok" in n:
                    put("bttsNo", v)

        # Goal totals. Market names/choice names differ by feed, so both
        # market and choice text are inspected.
        if ("over/under" in lname or "total" in lname or "goals" in lname
                or "üst" in lname or "alt" in lname):
            for c in choices:
                n = choice_name(c).lower()
                v = decimal_choice(c)
                line_match = re.search(r"([0-4](?:\.5)?)", (name + " " + n))
                if not line_match:
                    continue
                line = line_match.group(1)
                # 2.5 gibi decimal string; sadece istenen çizgiler.
                if line not in {"0.5","1.5","2.5","3.5","4.5"}:
                    continue
                is_over = any(x in n for x in ("over", "üst", "above"))
                is_under = any(x in n for x in ("under", "alt", "below"))
                prefix = "over" if is_over else ("under" if is_under else "")
                if prefix:
                    put(prefix + "_" + line.replace(".", "_"), v)

        # Half-time result
        if ("half time" in lname or "half-time" in lname or
                "first half result" in lname or "ilk yarı" in lname):
            for c in choices:
                n = choice_name(c).lower()
                v = decimal_choice(c)
                if n in ("1","home","home win","ev sahibi"):
                    put("ht1", v)
                elif n in ("x","draw","beraberlik"):
                    put("htX", v)
                elif n in ("2","away","away win","deplasman"):
                    put("ht2", v)

        # Half-time totals
        if ("first half" in lname or "1st half" in lname or "ilk yarı" in lname):
            for c in choices:
                n = choice_name(c).lower()
                v = decimal_choice(c)
                lm = re.search(r"([0-2](?:\.5)?)", (name + " " + n))
                if not lm:
                    continue
                line = lm.group(1)
                if line not in {"0.5","1.5","2.5"}:
                    continue
                if any(x in n for x in ("over", "üst", "above")):
                    put("htOver" + line.replace(".", ""), v)
                elif any(x in n for x in ("under", "alt", "below")):
                    put("htUnder" + line.replace(".", ""), v)

        # Half-time/full-time combinations
        if ("half time/full time" in lname or "half-time/full-time" in lname
                or "ht/ft" in lname or "iy/ms" in lname):
            mapping = {
                "1/1":"1_1","1/x":"1_X","1/2":"1_2",
                "x/1":"X_1","x/x":"X_X","x/2":"X_2",
                "2/1":"2_1","2/x":"2_X","2/2":"2_2",
            }
            for c in choices:
                n = choice_name(c).lower().replace(" ", "")
                v = decimal_choice(c)
                if n in mapping:
                    put("htft" + mapping[n].lower().replace("_",""), v)

    # Convert our internal key style to exactly what index.html reads.
    aliases = {}
    for k, v in markets.items():
        aliases[k] = v
    return aliases

def parse_event_form(events, team_id):
    out = []
    for e in events:
        ht = (e.get("homeTeam") or {})
        at = (e.get("awayTeam") or {})
        hs = (e.get("homeScore") or {}).get("current")
        aws = (e.get("awayScore") or {}).get("current")
        if hs is None or aws is None:
            continue
        is_home = ht.get("id") == team_id
        if not is_home and at.get("id") != team_id:
            continue
        result = "W" if (is_home and hs > aws) or ((not is_home) and aws > hs) else \
                 ("D" if hs == aws else "L")
        out.append({
            "date": datetime.fromtimestamp(
                e.get("startTimestamp", 0), timezone.utc
            ).strftime("%Y-%m-%d") if e.get("startTimestamp") else "",
            "home": ht.get("name",""),
            "away": at.get("name",""),
            "score": f"{hs}-{aws}",
            "result": result,
            "homeGoals": hs,
            "awayGoals": aws
        })
        if len(out) >= 5:
            break
    return out

def summarize_form(form):
    if not form:
        return {}
    total = len(form)
    hg = sum(x["homeGoals"] if x["home"] == form[0]["home"] else x["awayGoals"] for x in form)
    ag = sum(x["awayGoals"] if x["away"] == form[0]["away"] else x["homeGoals"] for x in form)
    # Daha güvenli: maç başına takımın attığı golü doğrudan belirle.
    goals_for, goals_against = [], []
    btts = over25 = 0
    for x in form:
        # Bu fonksiyon tek takım formunda ilk kaydın takımını referans alır.
        team = form[0]["home"] if form[0]["home"] else ""
        # çağıran taraf ayrıca düzeltme yapacağından burada sadece ortak metrikler.
        g1, g2 = x["homeGoals"], x["awayGoals"]
        btts += int(g1 > 0 and g2 > 0)
        over25 += int(g1 + g2 > 2)
    return {
        "bttsPct": round(btts / total * 100, 1),
        "over25Pct": round(over25 / total * 100, 1),
    }

def team_form_stats(events, team_id):
    form = parse_event_form(events, team_id)
    gf = ga = btts = over25 = 0
    for x in form:
        if x["home"] and x["home"] == x["home"]:  # explicit for readability
            # Determine side by event team ID is not retained; compare result:
            # We can infer from the event's home/away team id only if supplied.
            pass
    # Re-read with IDs for exact team averages.
    gf = ga = btts = over25 = 0
    for e in events:
        ht = e.get("homeTeam") or {}
        at = e.get("awayTeam") or {}
        hs = (e.get("homeScore") or {}).get("current")
        aws = (e.get("awayScore") or {}).get("current")
        if hs is None or aws is None:
            continue
        if ht.get("id") == team_id:
            a,b = hs,aws
        elif at.get("id") == team_id:
            a,b = aws,hs
        else:
            continue
        gf += a; ga += b
        btts += int(a > 0 and b > 0)
        over25 += int(a+b > 2)
    n = len(form)
    return form, {
        "homeGoalsAvg": round(gf/n,2) if n else None,
        "awayGoalsAvg": round(ga/n,2) if n else None,
        "bttsPct": round(btts/n*100,1) if n else None,
        "over25Pct": round(over25/n*100,1) if n else None,
    }

def build_sofa_analysis(home, away, match_time=""):
    event = find_sofa_event(home, away, match_time)
    if not event:
        return None

    event_id = event.get("id")
    if not event_id:
        return None

    result = {
        "sofascoreEventId": event_id,
        "home": (event.get("homeTeam") or {}).get("name", home),
        "away": (event.get("awayTeam") or {}).get("name", away),
        "markets": {},
    }

    # Odds
    try:
        odds_payload = sofa_get(f"/event/{event_id}/odds/1/all")
        result["markets"] = parse_sofa_markets(odds_payload)
    except Exception:
        try:
            odds_payload = sofa_get(f"/event/{event_id}/odds/1/featured")
            result["markets"] = parse_sofa_markets(odds_payload)
        except Exception:
            pass

    # Team history
    ht = event.get("homeTeam") or {}
    at = event.get("awayTeam") or {}
    if ht.get("id"):
        try:
            h_events = sofa_get(f"/team/{ht['id']}/events/last/0").get("events", [])
            hf, hs = team_form_stats(h_events, ht["id"])
            result["homeForm"] = hf
            result.setdefault("statistics", {}).update({
                "homeGoalsAvg": hs["homeGoalsAvg"],
                "homeBttsPct": hs["bttsPct"],
                "homeOver25Pct": hs["over25Pct"],
            })
        except Exception:
            pass

    if at.get("id"):
        try:
            a_events = sofa_get(f"/team/{at['id']}/events/last/0").get("events", [])
            af, ast = team_form_stats(a_events, at["id"])
            result["awayForm"] = af
            result.setdefault("statistics", {}).update({
                "awayGoalsAvg": ast["awayGoalsAvg"],
                "awayBttsPct": ast["bttsPct"],
                "awayOver25Pct": ast["over25Pct"],
            })
        except Exception:
            pass

    # Combined stats
    st = result.setdefault("statistics", {})
    btts_vals = [x for x in (st.get("homeBttsPct"), st.get("awayBttsPct")) if x is not None]
    over_vals = [x for x in (st.get("homeOver25Pct"), st.get("awayOver25Pct")) if x is not None]
    if btts_vals:
        st["bttsPct"] = round(sum(btts_vals)/len(btts_vals),1)
    if over_vals:
        st["over25Pct"] = round(sum(over_vals)/len(over_vals),1)

    # H2H
    try:
        h2h_payload = sofa_get(f"/event/{event_id}/h2h/events")
        h2h_events = h2h_payload.get("events") or h2h_payload.get("h2hEvents") or []
        h2h = []
        for e in h2h_events[:5]:
            hs = (e.get("homeScore") or {}).get("current")
            aws = (e.get("awayScore") or {}).get("current")
            if hs is None or aws is None:
                continue
            h2h.append({
                "date": datetime.fromtimestamp(
                    e.get("startTimestamp",0), timezone.utc
                ).strftime("%Y-%m-%d") if e.get("startTimestamp") else "",
                "home": (e.get("homeTeam") or {}).get("name",""),
                "away": (e.get("awayTeam") or {}).get("name",""),
                "score": f"{hs}-{aws}",
                "homeGoals": hs,
                "awayGoals": aws,
                "btts": "Var" if hs > 0 and aws > 0 else "Yok",
                "over25": "Üst" if hs + aws > 2 else "Alt",
            })
        result["h2h"] = h2h
    except Exception:
        result["h2h"] = []

    # Helpful aliases for the existing frontend.
    mk = result["markets"]
    for src, dst in [
        ("ms1","ms1"),("msx","msx"),("ms2","ms2"),
        ("over05","over_0_5"),("over_0_5","over_0_5"),("under05","under_0_5"),("under_0_5","under_0_5"),
        ("over15","over_1_5"),("over_1_5","over_1_5"),("under15","under_1_5"),("under_1_5","under_1_5"),
        ("over25","over_2_5"),("over_2_5","over_2_5"),("under25","under_2_5"),("under_2_5","under_2_5"),
        ("over35","over_3_5"),("over_3_5","over_3_5"),("under35","under_3_5"),("under_3_5","under_3_5"),
        ("over45","over_4_5"),("over_4_5","over_4_5"),("under45","under_4_5"),("under_4_5","under_4_5"),
        ("bttsYes","bttsYes"),("bttsNo","bttsNo"),
        ("ht1","ht1"),("htX","htX"),("ht2","ht2"),
        ("htOver05","htOver05"),("htUnder05","htUnder05"),
        ("htOver15","htOver15"),("htUnder15","htUnder15"),
        ("htOver25","htOver25"),("htUnder25","htUnder25"),
        ("htft11","htft11"),("htft1x","htft1x"),("htft12","htft12"),
        ("htftx1","htftx1"),("htftxx","htftxx"),("htftx2","htftx2"),
        ("htft21","htft21"),("htft2x","htft2x"),("htft22","htft22"),
    ]:
        if src in mk:
            result[dst] = mk[src]

    return result


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


@app.get("/api/match-analysis")
def match_analysis():
    home = (request.args.get("home") or "").strip()
    away = (request.args.get("away") or "").strip()
    match_time = (request.args.get("time") or "").strip()
    if not home or not away:
        return jsonify(ok=False, error="home ve away gerekli"), 400

    try:
        data = build_sofa_analysis(home, away, match_time)
        if not data:
            return jsonify(ok=False, error="Detay maç SofaScore üzerinde eşleştirilemedi"), 404
        return jsonify(ok=True, match=data)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 502


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
