"""每日后端：ASIC 做空数据 + 行情成交量 -> data/data.json（含全部历史比较与信号）"""
import re, io, json, os, pathlib, datetime as dt
import requests, pandas as pd, numpy as np, yfinance as yf
D = pathlib.Path("data"); D.mkdir(exist_ok=True)
H = {"User-Agent": "Mozilla/5.0"}
BASE = "https://download.asic.gov.au/short-selling/"
HIST = D / "short_history.csv"          # 累积的全部历史: date,code,pct
BACKFILL = int(os.getenv("BACKFILL", "250"))  # 首次回填的交易日数

def list_files():
    t = requests.get(BASE + "short-selling-data.json", headers=H, timeout=60).text
    names = sorted(set(re.findall(r"RR\d{8}-\d+-SSDailyAggShortPos\.csv", t)))
    if not names:  # 备用：从 ASIC 页面抓链接
        p = requests.get("https://www.asic.gov.au/regulatory-resources/markets/short-selling/short-position-reports-table/", headers=H, timeout=60).text
        names = sorted(set(re.findall(r"RR\d{8}-\d+-SSDailyAggShortPos\.csv", p)))
    return names

def read_asic(name):
    b = requests.get(BASE + name, headers=H, timeout=60).content
    s = b.decode("utf-16") if b[:2] in (b"\xff\xfe", b"\xfe\xff") else b.decode("utf-8-sig", "ignore")
    df = pd.read_csv(io.StringIO(s), sep=None, engine="python")
    code = next(c for c in df.columns if "code" in c.lower())
    pct = next(c for c in df.columns if "%" in c)
    out = pd.DataFrame({"code": df[code].astype(str).str.strip(), "pct": pd.to_numeric(df[pct], errors="coerce")})
    out["date"] = dt.datetime.strptime(name[2:10], "%Y%m%d").date().isoformat()
    return out.dropna()

def update_history():
    old = pd.read_csv(HIST) if HIST.exists() else pd.DataFrame(columns=["date", "code", "pct"])
    have = set(old["date"])
    todo = [n for n in list_files() if dt.datetime.strptime(n[2:10], "%Y%m%d").date().isoformat() not in have][-BACKFILL:]
    new = [read_asic(n) for n in todo]
    h = pd.concat([old] + new)[["date", "code", "pct"]].drop_duplicates(["date", "code"])
    h.to_csv(HIST, index=False); return h

def prices(codes):
    V, C = [], []
    for i in range(0, len(codes), 150):
        d = yf.download([c + ".AX" for c in codes[i:i+150]], period="6mo", group_by="column", progress=False, threads=True)
        if d.empty: continue
        V.append(d["Volume"]); C.append(d["Close"])
    return pd.concat(V, axis=1), pd.concat(C, axis=1)

def news(code):
    try:  # 尽力而为：ASX 公告接口（非官方，失败则忽略）
        r = requests.get(f"https://asx.com.au/asx/1/company/{code}/announcements?count=3&market_sensitive=false", headers=H, timeout=15).json()
        return [{"t": a["header"], "d": a["document_release_date"][:10], "u": a.get("url", "")} for a in r["data"]]
    except Exception: return []

def main():
    h = update_history()
    wide = h.pivot(index="date", columns="code", values="pct").sort_index()
    codes = list(wide.columns)
    vol, close = prices(codes)
    out = []
    for c in codes:
        s = wide[c].dropna()
        k = c + ".AX"
        if len(s) < 10 or k not in vol.columns: continue
        v = vol[k].dropna(); p = close[k].dropna()
        if len(v) < 25: continue
        av = v.iloc[-21:-1].mean(); avs = s.iloc[-31:-1].mean(); sdv = s.iloc[-31:-1].std() or 1e-6
        if av * p.iloc[-1] < 100_000: continue          # 过滤流动性极差的股票
        vr = v.iloc[-1] / av; sd = s.iloc[-1] - avs; z = sd / sdv
        px = (p.iloc[-1] / p.iloc[-2] - 1) * 100
        sig, sc = "hold", 0.0
        if vr >= 1.3 and z <= -1 and px > 0: sig, sc = "buy", (vr - 1) + abs(z)
        elif vr >= 1.2 and z >= 1 and px < 0: sig, sc = "sell", (vr - 1) + z
        out.append(dict(c=c, V=[int(x) for x in v.iloc[-40:]], S=[round(x, 3) for x in s.iloc[-40:]],
                        av=int(av), **{"as": round(avs, 3)}, vr=round(vr, 2), sd=round(sd, 3),
                        sp=round(sd / avs * 100 if avs else 0, 1), px=round(px, 2), z=round(z, 2), sig=sig, sc=round(sc, 2)))
    nw = {d["c"]: news(d["c"]) for d in sorted([d for d in out if d["sig"] != "hold"], key=lambda d: -d["sc"])[:20]}
    json.dump({"updated": dt.datetime.utcnow().isoformat() + "Z", "short_date": wide.index[-1], "stocks": out, "news": nw},
              open(D / "data.json", "w"), separators=(",", ":"))
    print("ok", len(out), "stocks")

if __name__ == "__main__": main()
