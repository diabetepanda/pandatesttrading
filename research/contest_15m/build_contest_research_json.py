#!/usr/bin/env python3
"""Build static JSON for the Signal Desk "Short-Term Strategy Research" (短線策略研究) tab.

Reads:
  - research/contest_15m/results.json (Round 1, commit edd472b)
  - research/contest_15m/results_round2.json (Round 2, commit f37d7ee)

Outputs:
  - data/contest_research.json
  - assets/contest_research.json
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def build_contest_research_dict() -> dict:
    repo_root = Path(__file__).resolve().parent.parent.parent
    r1_path = repo_root / "research" / "contest_15m" / "results.json"
    r2_path = repo_root / "research" / "contest_15m" / "results_round2.json"

    with open(r1_path, "r", encoding="utf-8") as f:
        r1 = json.load(f)
    with open(r2_path, "r", encoding="utf-8") as f:
        r2 = json.load(f)

    strategy_descriptions = [
        {
            "id": "A",
            "name": "A 跌深反轉",
            "description": "價格偏離 EMA20 超過 2.0~2.5×ATR 且收出吞噬或針狀反轉 K 線時逆勢進場，捕捉超跌反彈。",
        },
        {
            "id": "B",
            "name": "B 開盤區間突破",
            "description": "倫敦/紐約開盤前 1 小時震盪區間突破，且該區間小於 2.5×ATR 時順勢追單。",
        },
        {
            "id": "C",
            "name": "C 順勢回檔",
            "description": "1 小時 EMA50 判定大趨勢方向，15 分 K 回測 EMA20 區域收出確認 K 線順勢進場。",
        },
        {
            "id": "D",
            "name": "D 布林擠壓突破",
            "description": "布林帶寬壓縮至歷史前 10~15% 極窄區間後，帶量突破布林上下軌時順勢進場。",
        },
        {
            "id": "E",
            "name": "E 支撐壓力反彈",
            "description": "過去 60~100 根 K 線確認之關鍵擺動高低點（Pivot），價格觸碰後收出確認反彈 K 線時進場。",
        },
        {
            "id": "F",
            "name": "F 訂單塊 (Order Block)",
            "description": "大實體位移（≥1.5~2.0×ATR）前最後一根反向 K 線所形成的訂單塊，價格首次回測時進場。",
        },
        {
            "id": "G",
            "name": "G 掃停損反轉 (Liquidity Sweep)",
            "description": "假突破前高或前低（刺破流動性）後 1~2 根迅速收回區間內，逆向做空或做多。",
        },
    ]

    # Helper to parse round 2 strategy row
    def parse_r2_strat(sid: str, sdata: dict) -> dict:
        norm = sdata.get("oos_normal", {})
        stress = sdata.get("oos_stress", {})
        zero = sdata.get("oos_zero_cost", {})
        sim = sdata.get("weekly_contest_7d_step", {})
        return {
            "id": sid,
            "title": sdata.get("title", sid),
            "trades": norm.get("trades", 0),
            "win_rate_pct": norm.get("win_rate_pct", 0.0),
            "avg_win_r": norm.get("avg_win_r", 0.0),
            "avg_loss_r": norm.get("avg_loss_r", 0.0),
            "expectancy_r": norm.get("expectancy_r", 0.0),
            "profit_factor": norm.get("profit_factor", 0.0),
            "stress_pf": stress.get("profit_factor", 0.0),
            "zero_cost_pf": zero.get("profit_factor", 0.0),
            "max_drawdown_pct": norm.get("max_drawdown_pct", 0.0),
            "max_consec_losses": norm.get("max_consec_losses", 0),
            "passed": sdata.get("passed", False),
            "contest_sim": {
                "median_return_pct": sim.get("median_return_pct", 0.0),
                "p90_return_pct": sim.get("p90_return_pct", 0.0),
                "bust_rate_pct": sim.get("bust_rate_pct", 0.0),
            },
        }

    strats_1h = [
        parse_r2_strat(sid, r2["timeframes"]["1h"]["strategies"][sid])
        for sid in ["A", "B", "C", "D", "E", "F", "G"]
        if sid in r2["timeframes"]["1h"]["strategies"]
    ]

    strats_15m = [
        parse_r2_strat(sid, r2["timeframes"]["15m"]["strategies"][sid])
        for sid in ["A", "B", "C", "D", "E", "F", "G"]
        if sid in r2["timeframes"]["15m"]["strategies"]
    ]

    # Round 1 strategies
    round1_list = []
    for sid in ["A", "B", "C", "D"]:
        if sid in r1.get("strategies", {}):
            sdata = r1["strategies"][sid]
            norm = sdata.get("oos_normal", {})
            stress = sdata.get("oos_stress", {})
            zero = sdata.get("oos_zero_cost", {})
            round1_list.append({
                "id": sid,
                "title": sdata.get("title", sid),
                "trades": norm.get("trades", 0),
                "win_rate_pct": norm.get("win_rate_pct", 0.0),
                "expectancy_r": norm.get("expectancy_r", 0.0),
                "profit_factor": norm.get("profit_factor", 0.0),
                "stress_pf": stress.get("profit_factor", 0.0),
                "zero_cost_pf": zero.get("profit_factor", 0.0),
                "passed": False,
            })

    # Contest simulation table rows
    l2_baseline_sim = r2.get("l2_baseline", {}).get("weekly_contest_7d_step", {})
    contest_rows = [
        {
            "strategy": "Signal Desk L2（基準）",
            "timeframe": "1d",
            "median_return_pct": l2_baseline_sim.get("median_return_pct", 0.0),
            "p90_return_pct": l2_baseline_sim.get("p90_return_pct", 0.08),
            "bust_rate_pct": l2_baseline_sim.get("bust_rate_pct", 0.0),
            "is_baseline": True,
        }
    ]
    for s in strats_1h:
        contest_rows.append({
            "strategy": s["title"],
            "timeframe": "1h",
            "median_return_pct": s["contest_sim"]["median_return_pct"],
            "p90_return_pct": s["contest_sim"]["p90_return_pct"],
            "bust_rate_pct": s["contest_sim"]["bust_rate_pct"],
            "is_baseline": False,
        })
    for s in strats_15m:
        contest_rows.append({
            "strategy": s["title"],
            "timeframe": "15m",
            "median_return_pct": s["contest_sim"]["median_return_pct"],
            "p90_return_pct": s["contest_sim"]["p90_return_pct"],
            "bust_rate_pct": s["contest_sim"]["bust_rate_pct"],
            "is_baseline": False,
        })

    payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "takeaway": "我們測了 7 種短線策略、1 小時與 15 分鐘 K 線、最多 18 個商品，扣掉真實交易成本後，沒有一個有穩定優勢，所以目前不提供短線訊號。",
        "strategies_intro": strategy_descriptions,
        "methodology": {
            "period": "2023-09 至 2026-11（3.17 年），採用 Dukascopy 真實報價歷史數據，並嚴格過濾週末與休市零成交量之平盤 K 線。",
            "split": "前 70% 樣本內 (IS) 挑選最佳參數；後 30% 樣本外 (OOS) 嚴格凍結參數驗證，杜絕過度擬合。",
            "costs": "The5ers MT5 實盤極低成本：每邊名目 0.001%（萬分之 0.1）手續費 ＋ 各商品典型點差（如 SP500 0.8 點、XAUUSD 0.20 點、EURUSD 0.1 pip）。",
            "stress_and_zero": "加測兩組對照：點差 ×1.5 壓力測試，以及完全免點差、免手續費的「零成本」環境。",
            "criteria": "通過門檻：樣本外交易次數 ≥ 100 筆、獲利因子 (PF) ≥ 1.20、期望值 R > 0、點差 ×1.5 壓力測試 PF ≥ 1.05。",
            "definitions": {
                "pf": "獲利因子 (Profit Factor, PF) = 賺的總額 ÷ 賠的總額。大於 1.0 才有賺，等於 1.0 為打平，小於 1.0 為虧損。本站上線標準為 1.20。",
                "exp_r": "期望值 (Expectancy R) = 平均每筆交易賺賠幾倍停損。例如 +0.20R 代表每冒 100 元停損風險，長期平均能賺 20 元；負數代表每筆平均都在虧損。",
            },
        },
        "round2": {
            "tf_1h": {
                "symbols_count": 18,
                "symbols_list": "DAX40, JPN225, NAS100, SP500, UK100, US30, EURUSD, GBPUSD, USDJPY, USDCHF, USDCAD, AUDUSD, NZDUSD, XAUUSD, XAGUSD, WTI, BTCUSD, ETHUSD",
                "strategies": strats_1h,
            },
            "tf_15m": {
                "symbols_count": 5,
                "symbols_list": "SP500, EURUSD, XAUUSD, BTCUSD, ETHUSD",
                "strategies": strats_15m,
            },
        },
        "round1_summary": {
            "description": "第一輪研究（commit edd472b）使用較高之常規手續費（幣安每邊 0.05%）與點差，在 15 分 K 上測試代號 A–D 之 4 個短線方案。因高頻交易被手續費嚴重侵蝕，PF 僅落在 0.39 ~ 0.48，全數慘遭淘汰。",
            "strategies": round1_list,
        },
        "contest_sim": {
            "rules": "1 萬美元初始資金、單筆 4.5% 高風險、單日虧損達 5% 暫停當日交易、淨值跌破 9,000 美元即刻淘汰出局。OOS 期間每 7 天滾動測試（47~48 週）。",
            "quote": "偶爾一週能衝很高，但接近一半的機率被淘汰——那是運氣，不是策略。",
            "l2_note": "L2 基準一週中位數 0% 原因：L2 是日線長波段策略（55 日突破），一週內通常不出手（47 週共只出手 7 筆），因此不會頻繁承受震盪與手續費磨損，出局率為 0.0%。",
            "rows": contest_rows,
        },
        "learnings": [
            "成本吃掉短線大部分利潤：高頻短線交易受點差與手續費嚴重侵蝕。即便在 The5ers MT5 萬分之 0.1 極大手續費優惠下，15m 的 PF 依然只能在 0.83~0.91 徘徊。",
            "零成本下也只是打平，代表策略本身沒有統計優勢：在 0 手續費、0 點差的理想環境中，1h 與 15m 的零成本 PF 最高也僅 1.078 與 1.064，未達到穩定獲利門檻（PF ≥ 1.20）。",
            "訂單塊 (OB)／聰明錢 (SMC)／支撐壓力在短線上並無神奇魔力：坊間推崇的 SMC 或價格行為技術，在嚴格去未來函數、排除休市假棒的實證統計中，勝率僅 32%~39%，並未展現出勝過傳統指標的超額優勢。",
            "長週期趨勢策略（L1、L2、L4）仍是本站唯一的核心基石：長線日線策略靠著大盈虧比（移動停利捕捉 +3R~+8R）、低交易頻率（避開點差摩擦），在多週期回測皆穩定維持 PF 1.48~2.14。我們堅持只提供真正經過驗證的訊號。",
        ],
        "disclaimer": {
            "text": "研究結果僅供參考，過去表現不代表未來，不構成投資建議。",
            "github_links": [
                {
                    "title": "第二輪研究報告 (report_round2.md)",
                    "url": "https://github.com/diabetepanda/pandatesttrading/blob/main/research/contest_15m/report_round2.md",
                },
                {
                    "title": "第一輪研究報告 (report.md)",
                    "url": "https://github.com/diabetepanda/pandatesttrading/blob/main/research/contest_15m/report.md",
                },
                {
                    "title": "短線研究程式碼與設定 (research/contest_15m)",
                    "url": "https://github.com/diabetepanda/pandatesttrading/tree/main/research/contest_15m",
                },
            ],
        },
    }
    return payload


def main() -> None:
    repo_root = Path(__file__).resolve().parent.parent.parent
    data_dir = repo_root / "data"
    assets_dir = repo_root / "assets"
    data_dir.mkdir(parents=True, exist_ok=True)
    assets_dir.mkdir(parents=True, exist_ok=True)

    data = build_contest_research_dict()
    text = json.dumps(data, ensure_ascii=False, indent=2)

    data_file = data_dir / "contest_research.json"
    assets_file = assets_dir / "contest_research.json"

    data_file.write_text(text, encoding="utf-8")
    assets_file.write_text(text, encoding="utf-8")
    print(f"Successfully generated {data_file} and {assets_file}")


if __name__ == "__main__":
    main()
