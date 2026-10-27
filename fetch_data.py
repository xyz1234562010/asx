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
    try: d = yf.download([c + ".AX" for c in lst], period="1y", group_by="column", progress=False, threads=True)
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

UNIV_URLS = ["https://www.asx.com.au/asx/research/ASXListedCompanies.csv", "https://asx.com.au/asx/research/ASXListedCompanies.csv"]
def universe():
    """ASX 全部上市公司（官方名单）。失败则返回空，退回到 ASIC 名单。"""
    for u in UNIV_URLS:
        try:
            lines = requests.get(u, headers=H, timeout=60).text.splitlines()
            i = next(k for k, l in enumerate(lines) if l.lower().startswith("company name"))
            df = pd.read_csv(io.StringIO("\n".join(lines[i:])))
            out = {str(r["ASX code"]).strip(): (str(r["Company name"]).strip(), str(r.get("GICS industry group", "")).strip()) for _, r in df.iterrows()}
            print("universe ok:", len(out), "from", u); return out
        except Exception as e: print("universe failed:", u, e)
    return {}

FC = D / "fund_cache.json"; FUND_LIMIT = int(os.getenv("FUND_LIMIT", "400"))
def fund_one(c):
    try:
        i = yf.Ticker(c + ".AX").info
        g = lambda k: i.get(k) if isinstance(i.get(k), (int, float)) and i.get(k) == i.get(k) else None
        return dict(roe=g("returnOnEquity"), pm=g("profitMargins"), fcf=g("freeCashflow"), de=g("debtToEquity"),
                    pe=g("trailingPE"), pb=g("priceToBook"), beta=g("beta"), mc=g("marketCap"), t=dt.date.today().isoformat())
    except Exception: return None

def refresh_fund(order):
    """基本面缓存：每次只刷新 FUND_LIMIT 只（先补没有的，再刷新超过30天的），多跑几天即可覆盖全市场。"""
    from concurrent.futures import ThreadPoolExecutor
    cache = json.load(open(FC)) if FC.exists() else {}
    old = (dt.date.today() - dt.timedelta(days=30)).isoformat()
    todo = [c for c in order if c not in cache] + [c for c in order if c in cache and cache[c].get("t", "") < old]
    todo = todo[:FUND_LIMIT]
    with ThreadPoolExecutor(6) as ex:
        for c, r in zip(todo, ex.map(fund_one, todo)):
            if r: cache[c] = r
    json.dump(cache, open(FC, "w"), separators=(",", ":"))
    print("fundamentals cached:", len(cache), "refreshed this run:", len(todo)); return cache

def buffett(f):
    """巴菲特/芒格风格：优质(55)+价值(30)+安全(15)。参考 AQR《Buffett's Alpha》：价值、盈利质量、低Beta、低杠杆。"""
    sc, n = 0.0, 0
    cl = lambda x: max(0.0, min(1.0, x))
    if f.get("roe") is not None: n += 1; sc += 20 * cl(f["roe"] / 0.25)
    if f.get("pm") is not None: n += 1; sc += 15 * cl(f["pm"] / 0.20)
    if f.get("fcf") is not None: n += 1; sc += 10 * (f["fcf"] > 0)
    if f.get("de") is not None: n += 1; sc += 10 if f["de"] <= 50 else 5 if f["de"] <= 100 else 0
    if f.get("pe") is not None and f["pe"] > 0: n += 1; sc += 15 * cl((30 - f["pe"]) / 20)
    if f.get("pb") is not None and f["pb"] > 0: n += 1; sc += 15 * cl((5 - f["pb"]) / 3.5)
    if f.get("beta") is not None: sc += 8 * cl((1.6 - f["beta"]) / 1.0)
    if f.get("mc") is not None: sc += 7 * cl(f["mc"] / 5e9)
    return round(sc, 1) if n >= 4 else None

def quant(rows):
    """量化多因子（通用做法，并非幻方量化的真实模型）：动量、短期反转、量能、低波动、做空变化、趋势。"""
    df = pd.DataFrame(rows).set_index("c")
    z = lambda x: ((x - x.mean()) / (x.std() or 1)).clip(-3, 3)
    s = .30 * z(df.mom) - .10 * z(df.r1) + .15 * z(df.vt) - .20 * z(df.vol) - .15 * z(df.sd) + .10 * z(df.trend) - .5 * (df.sl > 10)
    return (s.rank(pct=True) * 100).round(1).to_dict()

def news(code):
    try:  # 尽力而为：ASX 公告接口（非官方，失败则忽略）
        r = requests.get(f"https://asx.com.au/asx/1/company/{code}/announcements?count=3&market_sensitive=false", headers=H, timeout=15).json()
        return [{"t": a["header"], "d": a["document_release_date"][:10], "u": a.get("url", "")} for a in r["data"]]
    except Exception: return []

def build(codes, wide, names, sectors, vols, closes, fund):
    out, raw = [], []
    for c in codes:
        sr = wide[c].dropna() if c in wide.columns else pd.Series(dtype=float)
        e = dict(c=c, n=names.get(c, ""), sec=sectors.get(c, ""), S=[], V=[], av=0, vr=0, px=0, sd=0, sp=0, z=0, sl=0,
                 sig="hold", sc=0, liq=0, bf=None, qf=None, f=None, m=0, r1=0)
        e["as"] = 0
        if len(sr) >= 1:
            base = sr.iloc[-31:-1]; avs = base.mean() if len(base) else sr.iloc[-1]
            sdv = (base.std() if len(base) > 1 else 0) or 1e-6; sd = sr.iloc[-1] - avs
            e.update(S=[round(x, 3) for x in sr.iloc[-40:]], sl=round(sr.iloc[-1], 2), sd=round(sd, 3),
                     z=round(sd / sdv, 2) if len(sr) >= 10 else 0, sp=round(sd / avs * 100 if avs else 0, 1)); e["as"] = round(avs, 3)
        if c in vols:
            v, p = vols[c], closes[c]; av = v.iloc[-21:-1].mean()
            if av > 0 and len(p) > 1:
                vr = v.iloc[-1] / av; px = (p.iloc[-1] / p.iloc[-2] - 1) * 100; liq = av * p.iloc[-1] >= 100_000
                e.update(V=[int(x) for x in v.iloc[-40:]], av=int(av), vr=round(vr, 2), px=round(px, 2), liq=int(liq))
                if liq and len(sr) >= 10 and abs(e["sd"]) >= 0.1:
                    if vr >= 1.3 and e["z"] <= -1 and px > 0: e["sig"], e["sc"] = "buy", round((vr - 1) + abs(e["z"]), 2)
                    elif vr >= 1.2 and e["z"] >= 1 and px < 0: e["sig"], e["sc"] = "sell", round((vr - 1) + e["z"], 2)
                if liq and len(p) >= 130 and len(v) >= 60:
                    r1 = p.iloc[-1] / p.iloc[-21] - 1; mom = p.iloc[-21] / p.iloc[-126] - 1
                    e["m"], e["r1"] = round(mom * 100, 1), round(r1 * 100, 1)
                    raw.append(dict(c=c, mom=mom, r1=r1, vt=float(np.log(max(v.iloc[-5:].mean(), 1) / max(v.iloc[-60:].mean(), 1))),
                                    vol=p.pct_change().iloc[-60:].std(), sd=e["sd"], sl=e["sl"], trend=p.iloc[-1] / p.iloc[-50:].mean() - 1))
        if c in fund:
            f = fund[c]; e["f"] = {k: (round(f[k], 3) if f.get(k) is not None else None) for k in ("roe", "pm", "de", "pe", "pb")}
            e["bf"] = buffett(f)
        out.append(e)
    if len(raw) >= 30:
        q = quant(raw)
        for e in out:
            if e["c"] in q: e["qf"] = q[e["c"]]
    return out

def main():
    h = update_history()
    wide = h.pivot(index="date", columns="code", values="pct").sort_index()
    names = h.dropna(subset=["name"]).drop_duplicates("code", keep="last").set_index("code")["name"].to_dict()
    uni = universe()
    names.update({k: v[0] for k, v in uni.items()}); sectors = {k: v[1] for k, v in uni.items()}
    codes = sorted(set(uni) | set(wide.columns))
    print("total codes:", len(codes), "(ASX list:", len(uni), "ASIC:", len(wide.columns), ")")
    vols, closes = prices(codes)
    order = sorted(vols, key=lambda c: -(vols[c].iloc[-21:].mean() * closes[c].iloc[-1]))
    fund = refresh_fund(order)
    out = build(codes, wide, names, sectors, vols, closes, fund)
    top = sorted([d for d in out if d["sig"] != "hold"], key=lambda d: -d["sc"])[:20]
    nw = {d["c"]: news(d["c"]) for d in top}
    json.dump({"updated": dt.datetime.utcnow().isoformat() + "Z", "short_date": wide.index[-1], "stocks": out, "news": nw},
              open(D / "data.json", "w"), separators=(",", ":"))
    print("ok", len(out), "stocks;", sum(d["liq"] for d in out), "ranked;", sum(1 for d in out if d["V"]), "with price;",
          sum(1 for d in out if d["bf"] is not None), "with buffett;", sum(1 for d in out if d["qf"] is not None), "with quant")

if __name__ == "__main__": main()
