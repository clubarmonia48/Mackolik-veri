from flask import Flask, jsonify, send_from_directory
import requests, re, os
from bs4 import BeautifulSoup
from datetime import datetime

app=Flask(__name__, static_folder=".")
UA="Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36 Chrome/120 Mobile Safari/537.36"

def parse_matches():
    url="https://www.mackolik.com/iddaa"
    r=requests.get(url,headers={"User-Agent":UA,"Accept-Language":"tr-TR,tr;q=0.9"},timeout=20)
    r.raise_for_status()
    soup=BeautifulSoup(r.text,"html.parser")
    out=[]
    # Sayfa yapısı değişebildiği için metin tabanlı güvenli bir ilk ayrıştırıcı.
    for a in soup.find_all("a",href=True):
        txt=" ".join(a.stripped_strings)
        href=a.get("href","")
        if "/mac/" not in href or " - " not in txt: continue
        teams=txt.split(" - ",1)
        if len(teams)!=2: continue
        home,away=teams[0].strip(),teams[1].strip()
        if len(home)>80 or len(away)>80: continue
        parent=a.parent
        block=" ".join(parent.stripped_strings) if parent else txt
        nums=re.findall(r"\b\d+(?:[.,]\d{2})\b",block)
        vals=[n.replace(",",".") for n in nums[-6:]]
        item={"home":home,"away":away,"league":"","time":"","ms1":None,"msx":None,"ms2":None,"over25":None}
        if len(vals)>=3:
            item["ms1"],item["msx"],item["ms2"]=vals[:3]
        if len(vals)>=5: item["over25"]=vals[4]
        out.append(item)
    # duplicate team pairs
    seen=set(); clean=[]
    for x in out:
        k=(x["home"],x["away"])
        if k not in seen: seen.add(k); clean.append(x)
    return clean[:200]

@app.get("/")
def home(): return send_from_directory(".", "index.html")

@app.get("/api/health")
def health(): return jsonify(ok=True,time=datetime.utcnow().isoformat()+"Z")

@app.get("/api/matches")
def matches():
    try: return jsonify(ok=True,matches=parse_matches(),source="Mackolik",time=datetime.utcnow().isoformat()+"Z")
    except Exception as e: return jsonify(ok=False,error=str(e)),502

if __name__=="__main__":
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT","10000")))
