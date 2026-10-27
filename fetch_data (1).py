# -*- coding: utf-8 -*-
"""
文件名：fetch_data.py
功能：高并发抓取澳洲 ASX 全市场股票数据
包含：ASIC 最新做空头寸、历史做空变化率 (Z-Score突破)、Yahoo Finance 实时行情动量因子、以及 AQR 巴菲特 Alpha 经典实战量化打分模型。
"""

import io
import os
import re
import datetime as dt
from concurrent.futures import ThreadPoolExecutor
import requests
import pandas as pd
import numpy as np
import yfinance as yf

# 全局请求配置
BASE_URL = "https://download.asic.gov.au/short-selling/"
H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

def fetch_asic_short_data():
    """1. 抓取 ASIC 最新公布的官方做空头寸底稿"""
    print("[1/5] 正在连接 ASIC 官方数据库获取最新做空文件...")
    try:
        t = requests.get(BASE_URL + "short-selling-data.json", headers=H, timeout=20).text
        names = re.findall(r"RR\d{8}-\d+-SSDailyAggShortPos\.csv", t)
        if names:
            latest_names = sorted(names)[-5:] # 取最近5个用于计算短期变化率
        else:
            raise ValueError("未在ASIC索引中找到符合标准的CSV文件")
        
        target_file = latest_names[-1]
        print(f" -> 正在下载最新做空快照: {target_file}")
        r = requests.get(BASE_URL + target_file, headers=H, timeout=30)
        s = r.content.decode("utf-16") if r.content.startswith(b"\xff\xfe") else r.content.decode("utf-8-sig", "ignore")
        df = pd.read_csv(io.StringIO(s), sep=None, engine="python")
        
        code_col = next(c for c in df.columns if "code" in c.lower())
        pct_col = next(c for c in df.columns if "%" in c)
        name_col = next((c for c in df.columns if "product" in c.lower() and "code" not in c.lower()), None)
        
        latest_df = pd.DataFrame({
            "code": df[code_col].astype(str).str.strip(),
            "short_pct": pd.to_numeric(df[pct_col], errors="coerce"),
            "company_name": df[name_col].astype(str).str.strip() if name_col else ""
        }).dropna()
        
        return latest_df, latest_names
    except Exception as e:
        print(f"❌ ASIC 数据抓取失败: {e}")
        return pd.DataFrame(), []

def calculate_short_zscore(latest_df, latest_names):
    """2. 补充计算做空头寸的短期变化率 (5日 Z-Score 突破)"""
    if len(latest_names) < 3:
        latest_df["short_zscore"] = 0.0
        return latest_df
    
    print("[2/5] 正在并发交叉计算近5期做空历史的滚动 Z-Score 变化率...")
    history_data = {}
    
    def fetch_hist_file(name):
        try:
            r = requests.get(BASE_URL + name, headers=H, timeout=20)
            s = r.content.decode("utf-16") if r.content.startswith(b"\xff\xfe") else r.content.decode("utf-8-sig", "ignore")
            df_hist = pd.read_csv(io.StringIO(s), sep=None, engine="python")
            c_col = next(c for c in df_hist.columns if "code" in c.lower())
            p_col = next(c for c in df_hist.columns if "%" in c)
            return name, df_hist[[c_col, p_col]].rename(columns={c_col: "code", p_col: "pct"})
        except:
            return name, None

    with ThreadPoolExecutor(max_workers=5) as ex:
        res = ex.map(fetch_hist_file, latest_names)
        for name, data in res:
            if data is not None:
                history_data[name] = data

    combined = pd.concat(history_data.values(), ignore_index=True)
    stats = combined.groupby("code")["pct"].agg(["mean", "std"]).reset_index()
    stats["std"] = stats["std"].replace(0, 1e-6)
    
    merged = pd.merge(latest_df, stats, on="code", how="left")
    merged["short_zscore"] = ((merged["short_pct"] - merged["mean"]) / merged["std"]).round(2)
    return merged.drop(columns=["mean", "std"]).fillna({"short_zscore": 0.0})

def fetch_asx_universe():
    """3. 抓取 ASX 官方全市场上市公司代码与行业分类列表"""
    print("[3/5] 正在同步 ASX 官方最新的全市场行业分类大纲...")
    urls = ["https://www.asx.com.au/asx/research/ASXListedCompanies.csv", "https://asx.com.au/asx/research/ASXListedCompanies.csv"]
    for u in urls:
        try:
            lines = requests.get(u, headers=H, timeout=15).text.splitlines()
            idx = next(k for k, l in enumerate(lines) if l.lower().startswith("company name"))
            df = pd.read_csv(io.StringIO("\n".join(lines[idx:])))
            df.columns = [c.strip().lower() for c in df.columns]
            return df.rename(columns={"asx code": "code", "company name": "full_name", "gics industry group": "sector"})
        except Exception:
            continue
    return pd.DataFrame()

def get_market_and_fundamental_factors(code):
    """4. 并发底盘：单只股票实时行情 + 动量因子 + 巴菲特 Alpha 基本面提炼"""
    try:
        ticker = yf.Ticker(f"{code}.AX")
        info = ticker.info
        hist = ticker.history(period="130d")
        if hist.empty or len(hist) < 25: 
            return None
        
        latest_close = hist['Close'].iloc[-1]
        prev_close = hist['Close'].iloc[-2]
        daily_return = ((latest_close / prev_close) - 1) * 100
        recent_vol = hist['Volume'].iloc[-1]
        avg_vol = hist['Volume'].iloc[-30:-1].mean()
        vol_ratio = recent_vol / avg_vol if avg_vol > 0 else 1.0
        daily_turnover = recent_vol * latest_close
        
        r20 = ((hist['Close'].iloc[-1] / hist['Close'].iloc[-21]) - 1) * 100 if len(hist) >= 21 else 0.0
        mom_6m = ((hist['Close'].iloc[-21] / hist['Close'].iloc[-126]) - 1) * 100 if len(hist) >= 126 else 0.0
        
        g = lambda k: info.get(k) if isinstance(info.get(k), (int, float)) and info.get(k) == info.get(k) else None
        
        return {
            "code": code, "close": round(latest_close, 2), "daily_return": round(daily_return, 2),
            "vol_ratio": round(vol_ratio, 2), "daily_turnover": round(daily_turnover, 0),
            "momentum_20d_rev": round(r20, 2), "momentum_6m_trend": round(mom_6m, 2),
            "roe": g("returnOnEquity"), "pm": g("profitMargins"), "fcf_positive": 1.0 if (g("freeCashflow") or 0) > 0 else 0.0,
            "de": g("debtToEquity"), "pe": g("trailingPE"), "pb": g("priceToBook"), "beta": g("beta"), "mc": g("marketCap")
        }
    except:
        return None

def calculate_buffett_alpha(f):
    """5. 实战经典：对冲基金 AQR 经典《Buffett's Alpha》全因子量化打分模型"""
    score = 0.0
    cl = lambda x: max(0.0, min(1.0, x))
    
    if f.get("roe") is not None: score += 20 * cl(f["roe"] / 0.25)
    if f.get("pm") is not None: score += 15 * cl(f["pm"] / 0.20)
    
    if f.get("pe") is not None and f["pe"] > 0: score += 15 * cl((30 - f["pe"]) / 20)
    if f.get("pb") is not None and f["pb"] > 0: score += 15 * cl((5 - f["pb"]) / 3.5)
    
    if f.get("fcf_positive") is not None: score += 10 * f["fcf_positive"]
    if f.get("de") is not None: score += 10 if f["de"] <= 50 else 5 if f["de"] <= 100 else 0
        
    if f.get("beta") is not None: score += 15 * cl((1.5 - f["beta"]) / 1.0)
    
    return round(score, 1)

def main():
    print("==================================================================")
    print("🚀 开始执行全因子多维度量化选股数据提取自动化任务...")
    print("==================================================================")
    
    short_df, latest_names = fetch_asic_short_data()
    if short_df.empty:
        print("❌ 核心做空数据抓取为空，流程提前终止。")
        return
    short_df = calculate_short_zscore(short_df, latest_names)
    
    universe_df = fetch_asx_universe()
    if not universe_df.empty:
        short_df = pd.merge(short_df, universe_df[['code', 'full_name', 'sector']], on='code', how='left')
    else:
        short_df['full_name'] = short_df['company_name']
        short_df['sector'] = "未知行业"
    
    filtered_pool = short_df[short_df['short_pct'] >= 1.0]
    target_codes = filtered_pool['code'].tolist()
    print(f"[4/5] 穿透池锁定 {len(target_codes)} 只做空异动目标股，开始高并发拉取实时行情与基本面...")
    
    market_results = []
    with ThreadPoolExecutor(max_workers=16) as executor:
        results = executor.map(get_market_and_fundamental_factors, target_codes)
        for i, r in enumerate(results):
            if r: 
                market_results.append(r)
            if (i + 1) % 50 == 0:
                print(f" -> 已完成 {i + 1} 只个股的衍生因子和基本面数据穿透提取")
                
    if not market_results:
        print("❌ 未能成功提取到任何个股的实时行情数据。")
        return
        
    market_df = pd.DataFrame(market_results)
    
    print("[5/5] 行情及基本面对接成功，开始对全维度矩阵注入 AQR 实战多因子打分系统...")
    final_df = pd.merge(filtered_pool, market_df, on='code', how='inner')
    final_df['buffett_alpha_score'] = final_df.apply(calculate_buffett_alpha, axis=1)
    
    final_df = final_df[final_df['daily_turnover'] >= 100000]
    final_df = final_df.sort_values(by=['short_pct', 'buffett_alpha_score'], ascending=[False, False])
    
    output_file = "asx_quant_signals.csv"
    final_df.to_csv(output_file, index=False, encoding="utf-8-sig")
    print("==================================================================")
    print(f"🎉 自动化提取大功告成！全因子量化清洗后的精细底数已成功保存至：")
    print(f" 👉 👉 【 {os.path.abspath(output_file)} 】 👈 👈")
    print("==================================================================")

if __name__ == '__main__':
    main()
