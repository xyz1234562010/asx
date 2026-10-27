"""每日后端：ASIC 做空数据 + 行情成交量 -> data/data.json（含全部历史比较与信号）"""
import re, io, json, os, pathlib, datetime as dt
import requests, pandas as pd, numpy as np, yfinance as yf
D = pathlib.Path("data"); D.mkdir(exist_ok=True)
H = {"User-Agent": "Mozilla/5.0"}
BASE = "https://download.asic.gov.au/short-selling/"
HIST = D / "short_history.csv"          # 累积的全部历史: date,code,pct
BACKFILL = int(os.getenv("BACKFILL", "200"))  # 首次回填的交易日数

def parse(b, name):
    s = b.decode("utf-16") if b[:2] in (b"\xff\xfe", b"\xfe\xff") else b.decode("utf-8-sig", "ignore")
    df = pd.read_csv(io.StringIO(s), sep=None, engine="python")
    code = next(c for c in df.columns if "code" in c.lower())
    pct = next(c for c in df.columns if "%" in c)
    nm = next((c for c in df.columns if "product" in c.lower() and "code" not in c.lower()), None)
    out = pd.DataFrame({"code": df[code].astype(str).str.strip(), "pct": pd.to_numeric(df[pct], errors="coerce"),
                        "name": df[nm].astype(str).str.strip() if nm else ""})
    out["date"] = dt.datetime.strptime(name[2:10], "%Y%m%d").date().isoformat()
    return out.dropna()

def get_day(name):
    try:
        r = requests.get(BASE + name, headers=H, timeout=60)
        if r.status_code != 200: return None
        return parse(r.content, name)
    except Exception: return None

def update_history():
    old = pd.read_csv(HIST) if HIST.exists() else pd.DataFrame(columns=["date", "code", "pct", "name"])
    if "name" not in old.columns: old["name"] = ""
    have = set(old["date"])
    back = 12 if len(have) else BACKFILL
    names = set()
    try:  # 若索引文件可用，则一并使用
        t = requests.get(BASE + "short-selling-data.json", headers=H, timeout=60).text
        names |= set(re.findall(r"RR\d{8}-\d+-SSDailyAggShortPos\.csv", t))
        print("index json ok, files:", len(names))
    except Exception as e: print("index json failed:", e)
    today = dt.date.today()
    for i in range(back):  # 直接按日期猜文件名（工作日）
        d = today - dt.timedelta(days=i)
        if d.weekday() < 5: names.add(f"RR{d:%Y%m%d}-001-SSDailyAggShortPos.csv")
    dd = lambda n: dt.datetime.strptime(n[2:10], "%Y%m%d").date()
    names = sorted(n for n in names if dd(n).isoformat() not in have or dd(n) >= today - dt.timedelta(days=12))  # 新日期 + 最近12天（刷新公司名称）
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(8) as ex: got = [g for g in ex.map(get_day, names) if g is not None]
    print("tried", len(names), "files, downloaded", len(got))
    h = pd.concat([old] + got, ignore_index=True)[["date", "code", "pct", "name"]].drop_duplicates(["date", "code"], keep="last")
    if h.empty: raise SystemExit("ASIC 数据一个都没下载到：请把这段日志截图给 Claude")
    h.to_csv(HIST, index=False); return h

def grab(lst, vols, closes):
    try: d = yf.download([c + ".AX" for c in lst], period="6mo", group_by="column", progress=False, threads=True)
    except Exception: return
    if d is None or d.empty: return
    v, c = d["Volume"], d["Close"]
    if isinstance(v, pd.Series): v, c = v.to_frame(lst[0] + ".AX"), c.to_frame(lst[0] + ".AX")
    for k in v.columns:
        sv = v[k].dropna()
        if len(sv) >= 25: vols[k[:-3]] = sv; closes[k[:-3]] = c[k].dropna()

def prices(codes):
    vols, closes = {}, {}
    for i in range(0, len(codes), 100): grab(codes[i:i+100], vols, closes)
    miss = [c for c in codes if c not in vols]
    print("price missing after 1st pass:", len(miss))
    for i in range(0, len(miss), 20): grab(miss[i:i+20], vols, closes)   # 补抓漏掉的
    print("price missing after retry:", len([c for c in codes if c not in vols]))
    return vols, closes

def news(code):
    try:  # 尽力而为：ASX 公告接口（非官方，失败则忽略）
        r = requests.get(f"https://asx.com.au/asx/1/company/{code}/announcements?count=3&market_sensitive=false", headers=H, timeout=15).json()
        return [{"t": a["header"], "d": a["document_release_date"][:10], "u": a.get("url", "")} for a in r["data"]]
    except Exception: return []

def build(wide, names, vols, closes):
    out = []
    for c in wide.columns:
        sr = wide[c].dropna()
        if len(sr) < 3: continue
        base = sr.iloc[-31:-1]
        avs = base.mean(); sdv = (base.std() if len(base) > 1 else 0) or 1e-6
        sd = sr.iloc[-1] - avs; z = sd / sdv
        e = dict(c=c, n=names.get(c, ""), S=[round(x, 3) for x in sr.iloc[-40:]], V=[], av=0, vr=0, px=0,
                 sd=round(sd, 3), sp=round(sd / avs * 100 if avs else 0, 1), z=round(z, 2), sig="hold", sc=0, liq=0)
        e["as"] = round(avs, 3)
        if c in vols:
            v = vols[c]; p = closes[c]
            av = v.iloc[-21:-1].mean()
            if av > 0:
                vr = v.iloc[-1] / av; px = (p.iloc[-1] / p.iloc[-2] - 1) * 100 if len(p) > 1 else 0
                liq = av * p.iloc[-1] >= 100_000
                e.update(V=[int(x) for x in v.iloc[-40:]], av=int(av), vr=round(vr, 2), px=round(px, 2), liq=int(liq))
                if liq and abs(sd) >= 0.1:   # 做空比例绝对变化至少0.1个百分点，过滤假信号
                    if vr >= 1.3 and z <= -1 and px > 0: e["sig"], e["sc"] = "buy", round((vr - 1) + abs(z), 2)
                    elif vr >= 1.2 and z >= 1 and px < 0: e["sig"], e["sc"] = "sell", round((vr - 1) + z, 2)
        out.append(e)
    return out

def main():
    h = update_history()
    wide = h.pivot(index="date", columns="code", values="pct").sort_index()
    names = h.dropna(subset=["name"]).drop_duplicates("code", keep="last").set_index("code")["name"].to_dict()
    vols, closes = prices(list(wide.columns))
    out = build(wide, names, vols, closes)
    top = sorted([d for d in out if d["sig"] != "hold"], key=lambda d: -d["sc"])[:20]
    nw = {d["c"]: news(d["c"]) for d in top}
    json.dump({"updated": dt.datetime.utcnow().isoformat() + "Z", "short_date": wide.index[-1], "stocks": out, "news": nw},
              open(D / "data.json", "w"), separators=(",", ":"))
    print("ok", len(out), "stocks;", sum(d["liq"] for d in out), "ranked;", sum(1 for d in out if d["V"]), "with price")

if __name__ == "__main__": main()
