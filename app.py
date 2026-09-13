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
            "url": ("https://www.mackolik.com"+href) if href.startswith("/") else href,
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
# Mackolik detail enrichment
# ---------------------------------------------------------------------------
def _find_match_url(tr):
    for a in tr.find_all('a', href=True):
        href=a.get('href','')
        if '/mac/' in href.lower() or '/match/' in href.lower():
            return ('https://www.mackolik.com'+href) if href.startswith('/') else href
    return None

def _pairs(lines,i,limit=12):
    out=[]
    pat=r'(1-X|1-2|X-2|1/1|1/X|1/2|X/1|X/X|X/2|2/1|2/X|2/2|1|X|2|Alt|Üst|Var|Yok|Evet|Hayır)\s+([0-9]+(?:[.,][0-9]{1,2})?|-)\b'
    for line in lines[i+1:i+1+limit]:
        if 'MBS' in line: continue
        for m in re.finditer(pat,line,re.I):
            pair=(m.group(1),m.group(2).replace(',','.'))
            if pair not in out: out.append(pair)
    return out

def parse_mackolik_markets(soup):
    lines=[_clean(x) for x in soup.stripped_strings if _clean(x)]
    mk={}
    market=re.compile(r'(Maç Sonucu|Çifte Şans|Karşılıklı Gol|[0-4][,.]5\s*Alt/Üst|1\. Yarı (?:Sonucu|Çifte Şans|[0-2][,.]5\s*Alt/Üst|Karşılıklı Gol)|İlk Yarı/Maç Sonucu)',re.I)
    for i,line in enumerate(lines):
        t=line.lower()
        if not market.search(line): continue
        pairs=_pairs(lines,i)
        if 'maç sonucu' in t and '1. yarı' not in t and '2. yarı' not in t and 'hnd' not in t:
            for k,v in pairs:
                try: f=float(v)
                except: continue
                if f<=1: continue
                if k=='1': mk.setdefault('ms1',f)
                elif k=='X': mk.setdefault('msx',f)
                elif k=='2': mk.setdefault('ms2',f)
        elif 'çifte şans' in t and '1. yarı' not in t:
            mp={'1-X':'doubleChance1X','1-2':'doubleChance12','X-2':'doubleChanceX2'}
            for k,v in pairs:
                if k in mp:
                    try: mk.setdefault(mp[k],float(v))
                    except: pass
        elif 'karşılıklı gol' in t and '1. yarı' not in t:
            for k,v in pairs:
                try: f=float(v)
                except: continue
                if k.lower()=='var' and f>1: mk.setdefault('bttsYes',f)
                if k.lower()=='yok' and f>1: mk.setdefault('bttsNo',f)
        elif re.search(r'\b[0-4][,.]5\s*alt/üst',t) and '1. yarı' not in t:
            lm=re.search(r'([0-4])[,.]5',t); linekey=lm.group(1)+'_5' if lm else None
            if linekey:
                for k,v in pairs:
                    try:f=float(v)
                    except:continue
                    if f<=1:continue
                    if k.lower()=='alt':mk.setdefault('under_'+linekey,f)
                    elif k.lower()=='üst':mk.setdefault('over_'+linekey,f)
        elif '1. yarı sonucu' in t:
            for k,v in pairs:
                try:f=float(v)
                except:continue
                if f<=1:continue
                if k=='1':mk.setdefault('ht1',f)
                elif k=='X':mk.setdefault('htX',f)
                elif k=='2':mk.setdefault('ht2',f)
        elif '1. yarı çifte şans' in t:
            mp={'1-X':'htdc1x','1-2':'htdc12','X-2':'htdcx2'}
            for k,v in pairs:
                if k in mp:
                    try:mk.setdefault(mp[k],float(v))
                    except:pass
        elif re.search(r'1\. yarı [0-2][,.]5\s*alt/üst',t):
            lm=re.search(r'1\. yarı\s+([0-2])[,.]5',t)
            if lm:
                n=lm.group(1)
                for k,v in pairs:
                    try:f=float(v)
                    except:continue
                    if f<=1:continue
                    if k.lower()=='alt':mk.setdefault('htUnder'+n+'5',f)
                    elif k.lower()=='üst':mk.setdefault('htOver'+n+'5',f)
        elif 'ilk yarı/maç sonucu' in t:
            mp={'1/1':'htft11','1/X':'htft1x','1/2':'htft12','X/1':'htftx1','X/X':'htftxx','X/2':'htftx2','2/1':'htft21','2/X':'htft2x','2/2':'htft22'}
            for k,v in pairs:
                if k in mp:
                    try:mk.setdefault(mp[k],float(v))
                    except:pass
    return mk

def parse_simple_results(soup):
    out=[]
    for tr in soup.find_all('tr'):
        cells=[_clean(c.get_text(' ',strip=True)) for c in tr.find_all(['td','th'])]
        row=' | '.join(cells)
        m=re.search(r'\b(\d+)\s*[-:]\s*(\d+)\b',row)
        if not m:continue
        teams=[c for c in cells if ' - ' in c]
        if not teams:continue
        a,b=teams[0].split(' - ',1)
        out.append({'date':'','home':a,'away':b,'score':f'{m.group(1)}-{m.group(2)}','result':''})
    seen=set();res=[]
    for x in out:
        k=(x['home'],x['away'],x['score'])
        if k not in seen:seen.add(k);res.append(x)
    return res[:5]

def build_mackolik_analysis(home,away,match_url):
    if not match_url:return None
    r=requests.get(match_url,headers={'User-Agent':UA,'Accept-Language':'tr-TR,tr;q=0.9,en;q=0.8'},timeout=(8,35)); r.raise_for_status()
    soup=BeautifulSoup(r.text,'html.parser')
    mk=parse_mackolik_markets(soup)
    res={'source':'Mackolik','matchUrl':match_url,'home':home,'away':away,'markets':mk}
    res.update(mk)
    results=parse_simple_results(soup)
    res['homeForm']=results[:5]
    res['awayForm']=[]
    res['h2h']=[]
    # Actual market-derived form percentages only; never invent odds/data.
    scores=[]
    for x in results:
        a,b=map(int,x['score'].split('-'));scores.append((a,b))
    if scores:
        res['statistics']={'bttsPct':round(sum(a>0 and b>0 for a,b in scores)/len(scores)*100,1),'over25Pct':round(sum(a+b>2 for a,b in scores)/len(scores)*100,1)}
    return res

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
    home=(request.args.get("home") or "").strip(); away=(request.args.get("away") or "").strip()
    match_url=(request.args.get("url") or "").strip()
    if not match_url:
        for m in _cache.get("matches",[]):
            if m.get("home")==home and m.get("away")==away and m.get("url"):
                match_url=m["url"]; break
    if not home or not away: return jsonify(ok=False,error="Maç takımları bulunamadı"),400
    if not match_url: return jsonify(ok=False,error="Mackolik maç bağlantısı bulunamadı"),404
    try:
        data=build_mackolik_analysis(home,away,match_url)
        return jsonify(ok=True,match=data) if data else (jsonify(ok=False,error="Mackolik detay alınamadı"),404)
    except Exception as e:
        return jsonify(ok=False,error=f"Mackolik detay alınamadı: {e}"),502

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
