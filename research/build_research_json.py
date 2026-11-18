#!/usr/bin/env python3
"""Build static JSON for the Signal Desk "In-Depth Research" (深度研究) tab.

Compiles data for:
  - Article 1: 「截長補短，能不能把策略組合得更好？」(Combo research: L1/L2/L3/L4/F4 & improvements)
  - Article 2: 「短線策略能拿去比賽嗎？」(Contest short-term research: 7 strategies on 1h/15m)

Outputs:
  - data/research.json
  - assets/research.json
  - (Also maintains data/contest_research.json & assets/contest_research.json for backward compatibility)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def build_research_dict() -> dict:
    repo_root = Path(__file__).resolve().parent.parent
    combo_res_path = repo_root / "research" / "combo" / "results.json"
    r1_path = repo_root / "research" / "contest_15m" / "results.json"
    r2_path = repo_root / "research" / "contest_15m" / "results_round2.json"

    with open(combo_res_path, "r", encoding="utf-8") as f:
        combo_res = json.load(f)
    with open(r1_path, "r", encoding="utf-8") as f:
        r1 = json.load(f)
    with open(r2_path, "r", encoding="utf-8") as f:
        r2 = json.load(f)

    # -------------------------------------------------------------
    # Article 1: 截長補短，能不能把策略組合得更好？
    # -------------------------------------------------------------
    baselines = combo_res.get("baselines", {})
    l2_base = baselines.get("L2", {})
    l1_base = baselines.get("L1", {})
    l4_base = baselines.get("L4", {})

    top5 = combo_res.get("top5_candidates", [])

    # Format lockbox comparison table
    lockbox_table = []

    # 1. Baseline L2
    l2_lb = l2_base.get("daily_10y_config", {}).get("lockbox", {})
    lockbox_table.append({
        "id": "L2_orig",
        "name": "原版 L2（基準：55 日突破 + 20 日低點移動停利）",
        "is_baseline": True,
        "expectancy_r": l2_lb.get("expectancy_r", 0.6516),
        "profit_factor": l2_lb.get("profit_factor", 3.399),
        "cagr_pct": l2_lb.get("cagr_pct", 35.56),
        "max_drawdown_pct": l2_lb.get("max_drawdown_pct", 3.17),
        "mar_ratio": l2_lb.get("mar_ratio", 11.21),
    })

    # 2. Baseline L1
    l1_lb = l1_base.get("daily_10y_config", {}).get("lockbox", {})
    lockbox_table.append({
        "id": "L1_orig",
        "name": "原版 L1（基準：20 日突破 + EMA20 移動停利）",
        "is_baseline": True,
        "expectancy_r": l1_lb.get("expectancy_r", 0.1459),
        "profit_factor": l1_lb.get("profit_factor", 1.631),
        "cagr_pct": l1_lb.get("cagr_pct", 15.73),
        "max_drawdown_pct": l1_lb.get("max_drawdown_pct", 6.73),
        "mar_ratio": l1_lb.get("mar_ratio", 2.34),
    })

    # 3. 5 Candidates
    for c in top5:
        lb = c.get("config_metrics", {}).get("lockbox", {})
        lockbox_table.append({
            "id": c["idea_id"],
            "name": c["title"],
            "is_baseline": False,
            "expectancy_r": lb.get("expectancy_r", 0.0),
            "profit_factor": lb.get("profit_factor", 0.0),
            "cagr_pct": lb.get("cagr_pct", 0.0),
            "max_drawdown_pct": lb.get("max_drawdown_pct", 0.0),
            "mar_ratio": lb.get("mar_ratio", 0.0),
        })

    # Kraken exam scenario simulation table
    kraken_table = [
        {
            "id": "L2_orig",
            "name": "原版 L2（基準：55 日突破）",
            "is_baseline": True,
            "pass_rate_pct": l2_base.get("scenario_kraken_exam", {}).get("pass_rate_pct", 93.04),
            "bust_rate_pct": l2_base.get("scenario_kraken_exam", {}).get("bust_rate_pct", 5.85),
            "median_days": l2_base.get("scenario_kraken_exam", {}).get("median_pass_days", 850.5),
            "notes": "通關率高達 93%，爆倉率僅 5.8%，單槽考試帳戶首選主力",
        },
        {
            "id": "4a_daily_consensus",
            "name": "候選 4：4a 長線策略合議（全同向加碼 1.25x）",
            "is_baseline": False,
            "pass_rate_pct": 89.42,
            "bust_rate_pct": 9.47,
            "median_days": 748.0,
            "notes": "加碼提高爆倉率至 9.5%，但達標天數比原版 L2 縮短 102 天",
        },
        {
            "id": "3d_mtf_weekly",
            "name": "候選 5：3d 週線一致過濾（20 週均線同側）",
            "is_baseline": False,
            "pass_rate_pct": 83.29,
            "bust_rate_pct": 15.60,
            "median_days": 720.0,
            "notes": "過濾逆勢單後交易筆數減少，遇震盪回撤時抗爆倉韌性降低",
        },
        {
            "id": "2a_chandelier",
            "name": "候選 3：2a 移動停利改 Chandelier（4.0×ATR）",
            "is_baseline": False,
            "pass_rate_pct": 59.89,
            "bust_rate_pct": 21.17,
            "median_days": 625.0,
            "notes": "急漲洗盤提早停利，在趨勢大年每筆獲利縮水，通關率跌破 60%",
        },
        {
            "id": "3a_trend_regime",
            "name": "候選 1：3a 趨勢強度過濾（SMA200 + ADX>20）",
            "is_baseline": False,
            "pass_rate_pct": 24.79,
            "bust_rate_pct": 52.09,
            "median_days": 329.0,
            "notes": "門檻過嚴錯失低位第一波起漲，通關率崩跌至 24.8%",
        },
        {
            "id": "L4_orig",
            "name": "原版 L4（基準：主流週線趨勢）",
            "is_baseline": True,
            "pass_rate_pct": l4_base.get("scenario_kraken_exam", {}).get("pass_rate_pct", 19.22),
            "bust_rate_pct": l4_base.get("scenario_kraken_exam", {}).get("bust_rate_pct", 0.0),
            "median_days": l4_base.get("scenario_kraken_exam", {}).get("median_pass_days", 754.0),
            "notes": "交易頻率極低、完全零爆倉，但在單槽考試中達標時間過長",
        },
        {
            "id": "L1_orig",
            "name": "原版 L1（基準：20 日突破）",
            "is_baseline": True,
            "pass_rate_pct": l1_base.get("scenario_kraken_exam", {}).get("pass_rate_pct", 18.66),
            "bust_rate_pct": l1_base.get("scenario_kraken_exam", {}).get("bust_rate_pct", 40.39),
            "median_days": l1_base.get("scenario_kraken_exam", {}).get("median_pass_days", 526.0),
            "notes": "交易頻繁且震盪磨損較多，爆倉率高達 40.4%",
        },
        {
            "id": "5a_multi_combo",
            "name": "候選 2：5a 多策略組合配置（各 20% 等權）",
            "is_baseline": False,
            "pass_rate_pct": 18.38,
            "bust_rate_pct": 51.53,
            "median_days": 633.5,
            "notes": "單槽限制下 L1/L3 小單佔用唯一持倉槽擋住 L2 大行情（劣幣驅逐良幣）",
        },
    ]

    article_combo = {
        "id": "combo",
        "slug": "combo",
        "title": "截長補短，能不能把策略組合得更好？",
        "subtitle": "多策略組合配置、進場時機、濾網與停利機制之 10 年三段嚴格盲測",
        "takeaway": "把幾個長線策略一起跑能把回撤砍半；其他『改良』看起來有進步，但大多是多冒風險或碰巧，考試帳戶仍用原版 L2。",
        "badge": "研究完成",
        "updated_at": "2026-11",
        "github_report": "https://github.com/diabetepanda/pandatesttrading/blob/main/research/combo/report.md",
        "github_code": "https://github.com/diabetepanda/pandatesttrading/tree/main/research/combo",
        "sections": {
            "anti_self_deception": {
                "title": "一、怎麼防止自欺：三段切分、61 組參數與統計檢定",
                "content": [
                    "研發策略最怕「過度擬合（Overfitting）」——只要調夠多次參數，隨便一個垃圾點子都能在歷史數據跑出暴利。為杜絕自欺欺人，本研究採取嚴格三道防線：",
                    "1. 資料三段切分：開發段（最早 60%，2016-01 ~ 2022-06，發想點子與初篩）、驗證段（25%，2022-07 ~ 2025-04，凍結挑選出的候選）、鎖定段（最後 15%，2025-05 ~ 2026-11，長達 1.5 年的完全盲測，全過程封箱、最後才解封跑一次）。",
                    "2. 嚴格多重測試扣罰（Deflated Sharpe Ratio, DSR）：全研究共測試 20 個點子、61 組參數組合。根據統計學，當測試 61 次時，即便純擲銅板也會碰巧出現年化夏普近 1.0 的漂亮結果。DSR 扣除了多重測試的運氣膨脹，只有真實技術機率 > 95%（碰巧機率 < 5%）才算通過。",
                    "3. 區塊自助抽樣（Block Bootstrap 檢定）：重抽 2,000 次日收益序列，檢定候選改良是否真正「在統計上顯著超越原版基準」。若 p-value > 0.05，代表所謂的超額報酬無法排除只是運氣碰巧。",
                ],
                "stats_summary": {
                    "total_ideas": 20,
                    "total_param_combos": 61,
                    "dev_period": "2016-01 至 2022-06 (~60%)",
                    "val_period": "2022-07 至 2025-04 (~25%)",
                    "lockbox_period": "2025-05 至 2026-11 (~15% 盲測)",
                },
            },
            "truly_useful": {
                "title": "二、真的有用：5a 多策略組合把回撤砍半，碰巧機率僅 1%",
                "content": [
                    "1. 5a 多策略組合配置（L1 + L2 + L3 + L4 + F4）：唯一通過 Deflated Sharpe 顯著性檢定的頂級資產配置方案。",
                    "回撤砍半、全天候穩健：日線趨勢（L1、L2）、週線趨勢（L4、F4）與動能策略（L3）彼此日報酬相關係數極低（-0.03 ~ +0.08），能完美互相填補回撤。在開發段與驗證段，最大回撤從原版 L2 的 22.6% 與 11.5% 大幅砍半至 10.2% 與 7.0%；在 1.5 年鎖定段更維持 +20.96% 年化與 2.87% 的極低回撤。",
                    "統計顯著：10 年全週期年化夏普高達 1.481。扣除 61 次多重測試扣罰後，DSR 真實技術機率達 98.85%（碰巧機率僅 1.15%）。",
                    "重要適用條件與致命限制：多策略組合的威力來自『多個策略各自同時持有不同倉位』。如果放在自有資金或網站主帳戶，這是首選配置；但若放在『一次只能持有一單』的海妖考試（Kraken Prop Exam），通關率會從原版 L2 的 93.0% 暴跌至 18.4%——因為高頻的 L1/L3 小單會佔用唯一的持倉槽，擋住 L2 捕捉數倍大行情的機會（劣幣驅逐良幣）。",
                ],
                "takeaway_pill": "多開倉帳戶必選 5a 組合（回撤砍半）；單槽考試帳戶切勿混用，務必單壓原版 L2。",
            },
            "apparent_improvement": {
                "title": "三、看起來有進步但要打折：策略合議實質加槓桿，週線過濾犧牲抗震力",
                "content": [
                    "1. 4a 長線策略合議（至少 2 策略同向才出手，3 策略全同向加碼 1.25 倍）：",
                    "表面看似暴利：在鎖定段年化報酬衝上 +47.25%（原版 L2 為 +35.56%）。但仔細檢視每筆期望值，僅從 +0.6516R 微幅升至 +0.6831R，超額利潤主要來自『加碼 1.25 倍槓桿』所承受的額外風險，而非策略選股勝率變高。",
                    "統計未達標：Bootstrap 檢定顯著超越基準的碰巧機率 p = 0.056 (5.6% > 5%)，未達統計顯著水準。",
                    "海妖考試隱患：雖然通關天數縮短 102 天，但爆倉率反而從 5.85% 升至 9.47%，在極限風控下容易被放大的槓桿反咬一口。",
                    "2. 3d 週線趨勢過濾（已收盤週線在 20 週均線同側才做日線突破）：",
                    "表面數據良好：鎖定段期望值達 +0.7128R、PF 3.555，剔除了逆週線大趨勢的假突破。",
                    "海妖考試大幅退步：通關率從 93.04% 降至 83.29%，爆倉率從 5.85% 激增至 15.60%！原因在於過濾掉交易後樣本數減少，單一連虧對資金池的傷害加劇，容錯空間大幅壓縮。",
                ],
            },
            "proven_useless": {
                "title": "四、實測無效的迷思：等回檔漏大魚、平半倉腰斬利潤、S2 正式退休",
                "content": [
                    "1. 迷思一：『日線突破太慢，等 1 小時 K 線回檔或掛限價單買便宜一點更好』→ 實測大敗。真正大級別突破行情通常一去不回頭。回檔 0.5×ATR 限價單（1c）成交率僅 23.8%，漏掉了高達 57.7%（30/52 筆）原本獲利超過 +2R 的超級暴利單！",
                    "2. 迷思二：『先平半倉落袋為安，剩下讓利潤奔跑』→ 實測大敗。部分止盈表面上勝率提高，但把真正的大贏家利潤腰斬，全週期期望值暴跌，純屬心理安慰劑。",
                    "3. 迷思三：『用短線 15 分 / 1 小時指標當日線濾網』→ 實測無效。短線充滿隨機噪音，加入日線系統只會增加虛假過濾與過度擬合。",
                    "4. S2 4小時突破救援實驗宣告失敗，S2 正式退休：即便利用户群組 3 之長均線與環境濾網搶救，S2 在鎖定段獲利因子 PF 僅 0.851（淨虧損 -15.27%），驗證段 PF 也僅 1.017。數據證實 4 小時震盪突破無法克服點差摩擦，S2 正式從實盤名單除役。",
                ],
            },
            "cautions": {
                "title": "五、注意事項：鎖定段大牛市背景，切勿把順風當常態",
                "content": [
                    "必須特別警惕：鎖定段（2025-05 ~ 2026-11，約 1.5 年）恰逢黃金歷史新高、加密貨幣爆發與美股長牛共振的罕見大多頭行情。",
                    "在此期間，所有順勢突破策略（包含原版 L2 PF 3.40、年化 +35.6%）均展現了遠高於歷史均值的爆發力。這段盲測證實了候選策略在大趨勢中能咬住行情，但絕不能將這段時間的極端高報酬視為未來的常態基準。",
                ],
            },
        },
        "tables": {
            "lockbox_comparison": lockbox_table,
            "kraken_scenarios": kraken_table,
        },
    }

    # -------------------------------------------------------------
    # Article 2: 短線策略能拿去比賽嗎？ (Ported from contest_research)
    # -------------------------------------------------------------
    strategy_descriptions = [
        {"id": "A", "name": "A 跌深反轉", "description": "價格偏離 EMA20 超過 2.0~2.5×ATR 且收出吞噬或針狀反轉 K 線時逆勢進場，捕捉超跌反彈。"},
        {"id": "B", "name": "B 開盤區民突破", "description": "倫敦/紐約開盤前 1 小時震盪區間突破，且該區間小於 2.5×ATR 時順勢追單。"},
        {"id": "C", "name": "C 順勢回檔", "description": "1 小時 EMA50 判定大趨勢方向，15 分 K 回測 EMA20 區域收出確認 K 線順勢進場。"},
        {"id": "D", "name": "D 布林擠壓突破", "description": "布林帶寬壓縮至歷史前 10~15% 極窄區間後，帶量突破布林上下軌時順勢進場。"},
        {"id": "E", "name": "E 支撐壓力反彈", "description": "過去 60~100 根 K 線確認之關鍵擺動高低點（Pivot），價格觸碰後收出確認反彈 K 線時進場。"},
        {"id": "F", "name": "F 訂單塊 (Order Block)", "description": "大實體位移（≥1.5~2.0×ATR）前最後一根反向 K 線所形成的訂單塊，價格首次回測時進場。"},
        {"id": "G", "name": "G 掃停損反轉 (Liquidity Sweep)", "description": "假突破前高或前低（刺破流動性）後 1~2 根迅速收回區間內，逆向做空或做多。"},
    ]

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

    article_short = {
        "id": "short_term",
        "slug": "contest-short",
        "title": "短線策略能拿去比賽嗎？",
        "subtitle": "為交易競賽進行之 15 分 K 與 1 小時 K 短線研究，兩輪共 7 種策略，扣除真實交易成本與嚴格樣本外驗證",
        "takeaway": "我們測了 7 種短線策略、1 小時與 15 分鐘 K 線、最多 18 個商品，扣掉真實交易成本後，沒有一個有穩定優勢，所以目前不提供短線訊號。",
        "badge": "全部未通過",
        "updated_at": "2026-11",
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

    full_payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "title": "深度研究 (In-Depth Research)",
        "subtitle": "Signal Desk 策略量化實證、多週期盲測與組合實驗報告",
        "disclaimer": "研究結果僅供參考，過去表現不代表未來，不構成投資建議。",
        "articles": [article_combo, article_short],
    }
    return full_payload


def main() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    data_dir = repo_root / "data"
    assets_dir = repo_root / "assets"
    data_dir.mkdir(parents=True, exist_ok=True)
    assets_dir.mkdir(parents=True, exist_ok=True)

    payload = build_research_dict()
    text = json.dumps(payload, ensure_ascii=False, indent=2)

    data_file = data_dir / "research.json"
    assets_file = assets_dir / "research.json"

    data_file.write_text(text, encoding="utf-8")
    assets_file.write_text(text, encoding="utf-8")
    print(f"Successfully generated {data_file} and {assets_file}")

    # Maintain contest_research.json for backward compatibility
    article_short = payload["articles"][1]
    legacy_payload = {
        "generated_at_utc": payload["generated_at_utc"],
        "takeaway": article_short["takeaway"],
        "strategies_intro": article_short["strategies_intro"],
        "methodology": article_short["methodology"],
        "round2": article_short["round2"],
        "round1_summary": article_short["round1_summary"],
        "contest_sim": article_short["contest_sim"],
        "learnings": article_short["learnings"],
        "disclaimer": article_short["disclaimer"],
    }
    legacy_text = json.dumps(legacy_payload, ensure_ascii=False, indent=2)
    (data_dir / "contest_research.json").write_text(legacy_text, encoding="utf-8")
    (assets_dir / "contest_research.json").write_text(legacy_text, encoding="utf-8")
    print("Maintained legacy contest_research.json files.")


if __name__ == "__main__":
    main()
