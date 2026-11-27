#!/usr/bin/env python3
"""Build static JSON for the Signal Desk "In-Depth Research" (深度研究) tab.

Compiles data chronologically for:
  - Article 1: 「短線策略能拿去比賽嗎？」(Contest short-term research: 7 strategies on 1h/15m)
  - Article 2: 「截長補短，能不能把策略組合得更好？」(Combo research: L1/L2/L3/L4/F4 & improvements)
  - Article 3: 「考試回撤規則放寬，結果會不一樣嗎？」(Exam drawdown sensitivity study)
  - Article 4: 「大戶／法人資金流回測（2026-11）」(Institutional / large-trader flow signals)
  - Article 5: 「定期定額 vs 單筆買進 vs 六種策略（10 年實證）」(DCA compare: 10y multi-asset & rolling backtest)
  - Article 6: 「美股與台股槓桿實證：倍率甜蜜點、借貸利率紅線與跌深加碼（20～34 年）」(Leverage study)
  - Article 7: 「每月 1 萬台幣定期定額：台美股與槓桿 ETF 全組合最佳配置（約 27～29 年）」(DCA allocation grid)

Outputs:
  - data/research.json
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
    exam_res_path = repo_root / "research" / "combo" / "exam_sensitivity_results.json"

    with open(combo_res_path, "r", encoding="utf-8") as f:
        combo_res = json.load(f)
    with open(r1_path, "r", encoding="utf-8") as f:
        r1 = json.load(f)
    with open(r2_path, "r", encoding="utf-8") as f:
        r2 = json.load(f)
    if exam_res_path.exists():
        with open(exam_res_path, "r", encoding="utf-8") as f:
            exam_res = json.load(f)
    else:
        exam_res = {}

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

    # -------------------------------------------------------------
    # Article 3: 考試回撤規則放寬，結果會不一樣嗎？ (Exam Sensitivity)
    # -------------------------------------------------------------
    sims = exam_res.get("simulations", {})

    best_combos_table = [
        {
            "dd_floor_pct": 10.0,
            "label": "10% 回撤底線",
            "strategy": "原版 L2",
            "risk_pct": 1.5,
            "position_limit": "一次一單 (1)",
            "pass_rate_pct": sims.get("L2|static|dd_10|r_1.5|pos_1|tgt_10", {}).get("full_period", {}).get("none", {}).get("pass_rate_pct", 99.72),
            "bust_rate_pct": sims.get("L2|static|dd_10|r_1.5|pos_1|tgt_10", {}).get("full_period", {}).get("none", {}).get("bust_rate_pct", 0.0),
            "median_days": sims.get("L2|static|dd_10|r_1.5|pos_1|tgt_10", {}).get("full_period", {}).get("none", {}).get("median_pass_days", 787.0),
            "pre_lockbox_pass_rate_pct": sims.get("L2|static|dd_10|r_1.5|pos_1|tgt_10", {}).get("pre_lockbox", {}).get("none", {}).get("pass_rate_pct", 100.0),
            "notes": "容錯空間極大，容許每單 1.0%~1.5% 風險加速通關，穩拿證書",
        },
        {
            "dd_floor_pct": 8.0,
            "label": "8% 回撤底線",
            "strategy": "原版 L2",
            "risk_pct": 1.5,
            "position_limit": "一次一單 (1)",
            "pass_rate_pct": sims.get("L2|static|dd_8|r_1.5|pos_1|tgt_10", {}).get("full_period", {}).get("none", {}).get("pass_rate_pct", 99.16),
            "bust_rate_pct": sims.get("L2|static|dd_8|r_1.5|pos_1|tgt_10", {}).get("full_period", {}).get("none", {}).get("bust_rate_pct", 0.56),
            "median_days": sims.get("L2|static|dd_8|r_1.5|pos_1|tgt_10", {}).get("full_period", {}).get("none", {}).get("median_pass_days", 783.5),
            "pre_lockbox_pass_rate_pct": sims.get("L2|static|dd_8|r_1.5|pos_1|tgt_10", {}).get("pre_lockbox", {}).get("none", {}).get("pass_rate_pct", 100.0),
            "notes": "主流考試標準配置（如 FTMO 10% / MFF 8%），勝率極佳",
        },
        {
            "dd_floor_pct": 5.0,
            "label": "5% 回撤底線",
            "strategy": "原版 L2",
            "risk_pct": 0.75,
            "position_limit": "一次一單 (1)",
            "pass_rate_pct": sims.get("L2|static|dd_5|r_0.75|pos_1|tgt_10", {}).get("full_period", {}).get("none", {}).get("pass_rate_pct", 98.61),
            "bust_rate_pct": sims.get("L2|static|dd_5|r_0.75|pos_1|tgt_10", {}).get("full_period", {}).get("none", {}).get("bust_rate_pct", 0.28),
            "median_days": sims.get("L2|static|dd_5|r_0.75|pos_1|tgt_10", {}).get("full_period", {}).get("none", {}).get("median_pass_days", 871.0),
            "pre_lockbox_pass_rate_pct": sims.get("L2|static|dd_5|r_0.75|pos_1|tgt_10", {}).get("pre_lockbox", {}).get("none", {}).get("pass_rate_pct", 99.64),
            "notes": "需將每單風險降至 0.5%~0.75%，過關天數拉長但爆倉率低",
        },
        {
            "dd_floor_pct": 3.0,
            "label": "3% 回撤底線",
            "strategy": "原版 L2",
            "risk_pct": 0.75,
            "position_limit": "一次一單 (1)",
            "pass_rate_pct": sims.get("L2|static|dd_3|r_0.75|pos_1|tgt_10", {}).get("full_period", {}).get("none", {}).get("pass_rate_pct", 93.04),
            "bust_rate_pct": sims.get("L2|static|dd_3|r_0.75|pos_1|tgt_10", {}).get("full_period", {}).get("none", {}).get("bust_rate_pct", 5.85),
            "median_days": sims.get("L2|static|dd_3|r_0.75|pos_1|tgt_10", {}).get("full_period", {}).get("none", {}).get("median_pass_days", 864.5),
            "pre_lockbox_pass_rate_pct": sims.get("L2|static|dd_3|r_0.75|pos_1|tgt_10", {}).get("pre_lockbox", {}).get("none", {}).get("pass_rate_pct", 94.66),
            "notes": "海妖極嚴格規則，容錯僅 3~4 次停損，需單押 L2 極限風控",
        },
    ]

    strat_order = [
        ("L2", "原版 L2"),
        ("4a_daily_consensus", "4a 日線合議"),
        ("3d_mtf_weekly", "3d 週線濾網"),
        ("2a_chandelier", "2a 吊燈通道"),
        ("5a_multi_combo", "5a 多策略組合"),
        ("L1", "原版 L1"),
    ]
    risks = [0.5, 0.75, 1.0, 1.5, 2.0]

    def _build_matrix_rows(pos_key: str) -> list[dict]:
        rows = []
        for sid, sname in strat_order:
            cells = {}
            for r in risks:
                k = f"{sid}|static|dd_10|r_{r}|{pos_key}|tgt_10"
                cell_data = sims.get(k, {}).get("full_period", {}).get("none", {})
                cells[str(r)] = {
                    "risk_pct": r,
                    "pass_rate_pct": cell_data.get("pass_rate_pct", 0.0),
                    "bust_rate_pct": cell_data.get("bust_rate_pct", 0.0),
                    "median_days": cell_data.get("median_pass_days"),
                }
            rows.append({
                "strategy_id": sid,
                "strategy_name": sname,
                "cells": cells,
            })
        return rows

    single_slot_rows = _build_matrix_rows("pos_1")
    multi_slot_rows = _build_matrix_rows("pos_3")

    article_exam = {
        "id": "exam_sensitivity",
        "slug": "exam-sensitivity",
        "title": "考試回撤規則放寬，結果會不一樣嗎？",
        "subtitle": "當 Prop Firm 回撤底線從 3% 放寬至 10%：6 大策略、多重每單風險與持倉上限之全維度敏感度模擬",
        "takeaway": "回撤放寬到 10% 主要是讓爆倉風險幾乎歸零，不太會讓過關變快；長線趨勢策略過考試大約要兩年，有 30 天時限的考試幾乎過不了。",
        "badge": "研究完成",
        "updated_at": "2026-11",
        "github_report": "https://github.com/diabetepanda/pandatesttrading/blob/main/research/combo/exam_sensitivity.md",
        "github_code": "https://github.com/diabetepanda/pandatesttrading/blob/main/research/combo/run_exam_sensitivity.py",
        "sections": {
            "what_we_simulated": {
                "title": "一、我們模擬了什麼？（白話名詞與研究架構）",
                "content": [
                    "本研究以真實歷史交易紀錄為基底，進行全維度「考試情境敏感度」模擬，覆蓋：",
                    "1. 測試維度：6 個策略（原版 L2、原版 L1、4a 日線合議、3d 週線濾網、5a 多策略組合、2a 吊燈通道）× 4 種回撤底線（3%、5%、8%、10%）× 5 種每單風險（0.5%、0.75%、1.0%、1.5%、2.0%）× 3 種持倉上限（一次一單 Single Slot、最多 3 單、不限持倉）× 2 種獲利目標（+8%、+10%）× 3 種時間限制（無時限、60 天、30 天）。",
                    "2. 挪動起點測試：自 2019 年 1 月至 2026 年 11 月，每隔 7 天滾動開跑一個獨立模擬帳戶（全期間共 359 梯次；另有排除大牛市之 Pre-Lockbox 盲測段共 281 梯次），真實統計不同時空背景進場時的通關率與爆倉率。",
                    "3. 白話名詞解說：",
                    "• 回撤底線（Max Drawdown）：考試帳戶允許的最大虧損上限。例如「10% 回撤底線」代表 1 萬美元起始資金若虧損至 9,000 美元（或從最高點回吐 10%），帳戶即被立刻強制收回淘汰。海妖考試（Kraken）要求極為嚴苛的 3% 回撤，而主流現代 Prop Firm（如 FTMO、The5ers 等）通常給予 8%~10% 的較大容錯空間。",
                    "• 每單風險（Risk per Trade）：每開一筆單如果打到停損，虧損金額佔總資產的比例。例如設定 1.0% 風險，意味著必須連續虧損 10 筆才會碰到 10% 底線；若開 2.0% 高風險，連輸 5 筆就會直接爆倉出局。",
                ],
            },
            "best_combos": {
                "title": "二、主要結果表：各回撤底線下的最佳配置",
                "content": [
                    "觀察重點（靜態回撤 Static DD、無時限、+10% 獲利目標）：",
                    "• 3% 嚴苛規則下：最佳組合為「原版 L2 ＋ 0.75% 風險 ＋ 一次一單」，通關率約 93.04%、爆倉率約 5.85%、中位通關天數約 864.5 天。",
                    "• 10% 放寬規則下：最佳組合為「原版 L2 ＋ 1.5% 風險 ＋ 一次一單」（或 1.0% 風險），通關率高達 99.72%、爆倉率 0.0%、中位通關天數約 787.0 天。",
                    "• 關鍵結論：兩者的中位通關時間都在 800 天左右（約 2 年～2.3 年），放寬回撤底線到 10% 並不能讓長線趨勢策略通關大幅變快，但最顯著的改變是「把爆倉出局的風險徹底壓到近乎零」，容錯從容度有天壤之別。",
                ],
            },
            "matrix_10pct": {
                "title": "三、10% 回撤策略矩陣：策略 × 每單風險（一次一單 vs 最多 3 單）",
                "content": [
                    "在主流 10% 回撤底線、無時限、+10% 目標下，檢驗 6 大策略在不同每單風險與持倉槽位下的通關率、爆倉率與中位通關天數：",
                ],
            },
            "three_findings": {
                "title": "四、三個重點發現",
                "findings": [
                    {
                        "title": "1. L2 不要同時開多單（多單同時虧損會致命）",
                        "text": "在 10% 回撤、每單 0.75% 風險下，如果維持「一次一單」，爆倉率為 0.0%；但只要放寬為「最多 3 單」，爆倉率會立刻飆升到 15.88%（若開到 1.0% 風險，爆倉率更達 25.35%）！這是因為大趨勢突破信號常在同一天或相近幾天於美股、黃金、原油等不同品種同時出現，持倉方向往往高度一致；一旦遭遇突發洗盤或假突破，3 筆單會集體被打停損，瞬間吃掉 2.5%~3% 以上淨值。",
                    },
                    {
                        "title": "2. 五策略組合（5a）只有在能開多單時才有用，而且不比單押 L2 快",
                        "text": "在「一次一單」限制下，5a 通關率只有 18%~28%（頻繁的小單佔用唯一名額擋住 L2）；只有在能同時開多單（最多 3 單）時，5a 的通關率才能回到 90.53%（0.75% 風險）。但即便放寬多單，通關中位天數仍需 658 天，並未比單押 L2 快。因此如果目標是考過考試，原版 L2 依然是最簡單純粹、心理負擔最小的首選。",
                    },
                    {
                        "title": "3. 有 30 天時限的考試，這些策略 98% 以上無法達標",
                        "text": "趨勢跟隨是低勝率（35%~40%）、大盈虧比（+3R~+8R）的長跑策略。在 30 天的極端短時限內，往往連一波完整趨勢都等不到，未達標超時率高達 98%~100%。如果因為時間壓力把每單風險開大，只會導致爆倉淘汰；考生挑選評測商時，務必選擇「無時間限制」或「至少 60 天以上」的現代評測商。",
                    },
                ],
            },
            "caveats": {
                "title": "五、要打折看的地方：歷史起點挪動與大趨勢段檢驗",
                "content": [
                    "1. 歷史挪動起點高度重疊：本次模擬是在 2019 至 2026 年同一段歷史中每隔 7 天設立一個新起點（共 359 梯次），不同起點的持倉時間高度重疊，並非「完全獨立的一百次新考試」。因此「99% 通關率」在統計上應理解為「很有機會」，而非「保證穩過」。",
                    "2. 排除 2025–2026 大牛市（Pre-Lockbox 盲測）：為了驗證不是被近期的黃金、加密貨幣長牛灌水，我們額外跑了只到 2025-05 的盲測段（281 梯次）。結果顯示，在 10% 底線下原版 L2、4a 等策略過關率依然高達 100.0%（爆倉率 0.0%），證實其在震盪或平淡市場中依然具備強大的風控與穿越週期能力，結論完全成立。",
                ],
            },
            "advice": {
                "title": "六、給考生的實戰操作建議（Cheat Sheet）",
                "points": [
                    "回撤 3% 的考試（如海妖極限風控）：單押原版 L2、每單風險 0.75%、務必保持一次一單（過關率約 93%、爆倉率約 6%）。",
                    "回撤 8%~10% 的考試（如主流 FTMO、The5ers 等）：單押原版 L2、每單風險 1.0%~1.5%、一次一單（過關率約 99%、爆倉率近 0%）。若想縮短時間可開最多 3 單，但每單風險嚴格壓低至 0.5%（過關率 92.5%、通關中位天數縮短至 615 天）。",
                    "嚴格避開 30 天短時限考試：長線趨勢策略無法在 1 個月內保證獲利，短時限考試只會淪為純賭博。",
                ],
            },
        },
        "tables": {
            "best_combos": best_combos_table,
            "single_slot_matrix": single_slot_rows,
            "multi_slot_matrix": multi_slot_rows,
        },
        "disclaimer": {
            "text": "研究結果僅供參考，過去表現不代表未來，不構成投資建議。",
            "github_links": [
                {
                    "title": "考試情境敏感度研究報告 (exam_sensitivity.md)",
                    "url": "https://github.com/diabetepanda/pandatesttrading/blob/main/research/combo/exam_sensitivity.md",
                },
                {
                    "title": "敏感度模擬程式碼 (run_exam_sensitivity.py)",
                    "url": "https://github.com/diabetepanda/pandatesttrading/blob/main/research/combo/run_exam_sensitivity.py",
                },
            ],
        },
    }

    # -------------------------------------------------------------
    # Article 4: 大戶／法人資金流回測（2026-11） (Flow Signals)
    # -------------------------------------------------------------
    article_flow = {
        "id": "flow_signals",
        "slug": "flow-signals",
        "title": "大戶／法人資金流回測（2026-11）",
        "subtitle": "台股三大法人／台指期／融資、美國 CFTC COT、OKX 合約與美國現貨 BTC ETF 共 57 項籌碼指標實證檢驗",
        "takeaway": "共測 57 項指標（913 項檢定），僅 2 項台股外資指標在「隔天（1 日）」有效，5 日以上全部失效；全域 BH 校正 q 約 0.42 與 0.20，嚴格統計標準下不顯著，只當隔天風險提示，不當進出場訊號。",
        "badge": "僅 2 項隔天提示",
        "updated_at": "2026-11",
        "github_report": "https://github.com/diabetepanda/pandatesttrading/blob/main/research/flow_signals/report.md",
        "github_code": "https://github.com/diabetepanda/pandatesttrading/tree/main/research/flow_signals",
        "sections": {
            "what_we_tested": {
                "title": "一、測了什麼？（三大市場 57 項籌碼指標與五道防自欺關卡）",
                "content": [
                    "很多市場說法喜歡看「法人買賣超」「美國 COT 大戶持倉」「交易所多空比與資金費率」來猜漲跌。我們把台股、美股期貨與加密貨幣共 57 項公開資金流指標放進同一套量化檢驗框架（合計 913 項統計檢定），確認到底有沒有真的預測力：",
                    "• 台股（^TWII 加權指數、2330.TW 台積電）：外資／投信／自營商現貨買賣超（單日、5 日、20 日、連買賣天數、60 日 z-score）、外資台指期淨未平倉（水位、單日變化、5 日變化、60 日區間位置）、大盤融資餘額變化與融資背離、台積電外資買賣超。",
                    "• 美國期貨 CFTC COT 持倉週報：S&P 500、那斯達克 100、黃金、WTI 原油與 CME 比特幣期貨的大戶淨部位佔未平倉比（Net % OI）及 156 週 COT Index。",
                    "• 加密貨幣（BTC / ETH）：OKX 大戶帳戶／持倉多空比（日線與 4 小時線）、主動買賣量比（Taker Buy/Sell）、永續合約資金費率（8h 結算）、未平倉量（OI）變化率，以及 Farside 美國現貨比特幣 ETF 每日淨流入。",
                    "• 防自欺機制：嚴格對齊公布時間（台股盤後公布只算隔天起算報酬、美國 COT 週五公布延至下週一開盤進場、BTC ETF 延至 D+1 起算，絕不偷看當日漲跌）；前 70% 樣本內／後 30% 樣本外切分；Newey-West 重疊報酬標準誤修正；全域 Benjamini-Hochberg (BH) 多重比較校正；以及剔除 2020-03 與 2022 極端行情測試。",
                ],
            },
            "data_periods": {
                "title": "二、資料期間與限制（樣本深度差異很大）",
                "content": [
                    "不同市場的公開免金鑰歷史長度差異很大，解讀結果時必須把樣本限制算進去：",
                    "• 台股現貨三大法人／融資／台積電（證交所 BFI82U / MI_MARGN / T86）：2015-01 至 2026-11（約 2,884 個交易日，接近 12 年完整多空循環）。",
                    "• 台股外資台指期淨未平倉（期交所 TAIFEX / FinMind）：公開免金鑰歷史從 2018-06 起（至 2026-11 約 2,056 個交易日，約 8.4 年）。",
                    "• 美國 CFTC COT 週報：S&P 500／NDX／黃金／原油自 2010-01 起（880 週）；CME 比特幣期貨自 2018-04 起（449 週）。",
                    "• 加密貨幣（OKX 公開端點歷史偏短）：日線資料約 2.7 年（最多約 998～999 日，2024-02 起）、4H 線約 5.5 個月（2026-06 起）、主動買賣量比日線僅約 99 日（2026-08 起）、永續合約資金費率（8h 結算）約 3 個月（2026-08 起，284 筆）；美國現貨 BTC ETF 自 2024-01 上市至今約 2.9 年（720 日），尚未經歷完整熊市。",
                ],
            },
            "effective_two": {
                "title": "三、唯二有效的台股隔天指標（5 日以上全面失效）",
                "content": [
                    "全部 57 項指標當中，只有 2 個台股外資指標在「隔天（1 日）」通過樣本內外同向、獨立事件數 ≥ 30 且單一事件勝率顯著優於同期基準的檢驗；一旦拉長到 5 日或 20 日以上，預測力立刻歸零失效：",
                ],
            },
            "useless_summary": {
                "title": "四、哪些指標沒用？（共 48 項淘汰）",
                "items": [
                    {
                        "group": "美國 CFTC COT（10 項測試，9 項沒用）",
                        "detail": "S&P 500、NDX、WTI 原油、CME 比特幣期貨的 Net % OI 與 156 週 COT Index，以及黃金 Net % OI，在樣本外多數方向反轉或無法通過多重比較校正。",
                    },
                    {
                        "group": "加密貨幣 OKX 與 ETF 單日流量（22 項測試，17 項沒用）",
                        "detail": "OKX 大戶帳戶／持倉多空比（1D 與 4H）、主動買賣量比（1D 與 4H）、永續合約資金費率（8h）、ETH OI 變化率與 BTC 4H OI 變化率、以及 BTC 現貨 ETF 單日淨流入，全數無穩定預測力（且 OKX 歷史偏短）。",
                    },
                    {
                        "group": "台股投信／自營商／個股／靜態水位（20 項沒用）",
                        "detail": "台股投信買賣超（單日／5日／20日／連買賣／z-score）、自營商買賣超（全部 5 種變形）、外資原始絕對金額與連買賣天數、外資台指期靜態淨未平倉水位與 60 日高低點突破、大盤融資餘額 5 日／20 日變化率、台積電（2330）外資買賣超，實測皆無穩定優勢。",
                    },
                ],
            },
            "reference_only": {
                "title": "五、哪些指標「僅供參考」？（共 7 項：樣本太短或多重校正未過）",
                "items": [
                    "A2 外資台指期淨未平倉 5 日變化（^TWII）：20 日預測期樣本內外方向一致且單一檢定 p=0.0091，但全域 BH 校正 q=0.561 未過門檻。",
                    "A3 台股融資增減與指數背離指標（^TWII）：1 日預測期單一檢定 p=0.0343，但 BH 校正 q=0.782 未過門檻。",
                    "B3 黃金期貨 COT Index（156 週百分位）：20 日預測期單一檢定 p=0.0494，但 BH 校正 q=0.943 未過門檻。",
                    "C3 BTC OKX 未平倉量變化率（OI Change %, 1D）：1 日呈現正相關（p=0.0198），但日線歷史僅約 2.7 年且 BH q=0.598，樣本太短。",
                    "C4 美國現貨 BTC ETF 5 日累計淨流入、20 日累計淨流入與淨流入 60 日 z-score：上市未滿 3 年且未經歷完整熊市（BH q=0.56～0.79），僅能當背景資金觀察，待累積更長週期再重評。",
                ],
            },
            "strict_caution": {
                "title": "六、重要提醒：嚴格統計標準下不顯著，只當隔天風險提示",
                "content": [
                    "這 2 個台股指標雖然在事件勝率標準上過關，但放在 913 項全域 Benjamini-Hochberg 多重比較校正下，q 值分別約為 0.42（A1）與 0.20（A2），以嚴格的 q < 0.05 統計標準來看並不顯著，整體證據偏弱。",
                    "因此，本站嚴格遵守「嚴格統計標準下不顯著，只當隔天風險提示，不當進出場訊號」的原則：只於台股（^TWII）及宏觀頁提供獨立的隔天風險提示小卡，絕不接進現有的 L2／L3 訊號評分或模擬帳戶自動交易邏輯。",
                ],
            },
        },
        "tables": {
            "effective_indicators": [
                {
                    "id": "A1_foreign_z60",
                    "name": "A1 外資現貨買賣超 60 日 z-score",
                    "market": "台股加權 (^TWII)",
                    "horizon": "僅隔天（1 日），5 日以上失效",
                    "trigger": "z > +1.5（異常大買）",
                    "oos_stat": "樣本外隔天上漲機率 72.88%（基準 56.58%，+16.3pp，N=59）",
                    "p_and_q": "p = 0.0023（全樣本）／ 樣本外 p = 0.0056；全域 BH q ≈ 0.42",
                    "note": "嚴格 q<0.05 不顯著，證據偏弱；只當隔天偏多提示",
                },
                {
                    "id": "A2_txf_1d_chg",
                    "name": "A2 外資台指期淨未平倉「單日變化」60 日 z-score",
                    "market": "台股加權 (^TWII)",
                    "horizon": "僅隔天（1 日），5 日以上失效",
                    "trigger": "z < -1.5，且排除結算日（每月第三個星期三）",
                    "oos_stat": "樣本外隔天平均 -0.74%，下跌機率 67.57%（基準 +23.0pp，N=37）",
                    "p_and_q": "全樣本 p = 0.00044（樣本外 p = 0.0033）；全域 BH q ≈ 0.20",
                    "note": "嚴格 q<0.05 不顯著，證據偏弱；只當隔天避險風險提示",
                },
            ],
        },
        "disclaimer": {
            "text": "嚴格統計標準（BH q<0.05）下不顯著，證據偏弱；只看隔天、僅作風險提示參考，不構成買賣訊號或投資建議。",
            "github_links": [
                {
                    "title": "完整資金流回測報告 (report.md)",
                    "url": "https://github.com/diabetepanda/pandatesttrading/blob/main/research/flow_signals/report.md",
                },
                {
                    "title": "回測程式碼與資料規格 (research/flow_signals)",
                    "url": "https://github.com/diabetepanda/pandatesttrading/tree/main/research/flow_signals",
                },
            ],
        },
    }

    # -------------------------------------------------------------
    # Article 5: 定期定額 vs 單筆買進 vs 六種策略（10 年實證） (DCA Compare)
    # -------------------------------------------------------------
    dca_res_path = repo_root / "research" / "dca_compare" / "results.json"
    if dca_res_path.exists():
        with open(dca_res_path, "r", encoding="utf-8") as f:
            dca_res = json.load(f)
    else:
        dca_res = {}

    fp_dca = dca_res.get("full_period_by_symbol", {})
    port_dca = dca_res.get("five_asset_combined_portfolio", {})
    r3_dca = dca_res.get("rolling_3y_by_symbol", {})
    r5_dca = dca_res.get("rolling_5y_by_symbol", {})

    core_plan_defs = [
        ("Lump_Sum", "單筆買進抱著", "被動長抱"),
        ("DCA_Monthly", "每月定期定額", "定期定額"),
        ("DCA_Dip_2x", "加碼版定期定額 (2x)", "逢低加碼"),
        ("L4", "策略 L4（主流週線趨勢）", "上線六策略"),
        ("L2", "策略 L2（精簡趨勢跟隨）", "上線六策略"),
        ("Hybrid_50DCA_50L4", "50% 定額 ＋ 50% L4 混合", "半倉混合"),
    ]

    core_summary_rows = []
    for pk, pname, pcat in core_plan_defs:
        core_summary_rows.append({
            "plan_key": pk,
            "plan_name": pname,
            "category": pcat,
            "twii": {
                "cagr": fp_dca.get("TWII", {}).get(pk, {}).get("xirr_pct", 0.0),
                "maxdd": fp_dca.get("TWII", {}).get(pk, {}).get("max_dd_pool_pct", 0.0),
                "mar": fp_dca.get("TWII", {}).get(pk, {}).get("mar_ratio", 0.0),
            },
            "sp500": {
                "cagr": fp_dca.get("SP500", {}).get(pk, {}).get("xirr_pct", 0.0),
                "maxdd": fp_dca.get("SP500", {}).get(pk, {}).get("max_dd_pool_pct", 0.0),
                "mar": fp_dca.get("SP500", {}).get(pk, {}).get("mar_ratio", 0.0),
            },
            "nas100": {
                "cagr": fp_dca.get("NAS100", {}).get(pk, {}).get("xirr_pct", 0.0),
                "maxdd": fp_dca.get("NAS100", {}).get(pk, {}).get("max_dd_pool_pct", 0.0),
                "mar": fp_dca.get("NAS100", {}).get(pk, {}).get("mar_ratio", 0.0),
            },
            "gold": {
                "cagr": fp_dca.get("XAUUSD", {}).get(pk, {}).get("xirr_pct", 0.0),
                "maxdd": fp_dca.get("XAUUSD", {}).get(pk, {}).get("max_dd_pool_pct", 0.0),
                "mar": fp_dca.get("XAUUSD", {}).get(pk, {}).get("mar_ratio", 0.0),
            },
            "btc": {
                "cagr": fp_dca.get("BTCUSDT", {}).get(pk, {}).get("xirr_pct", 0.0),
                "maxdd": fp_dca.get("BTCUSDT", {}).get(pk, {}).get("max_dd_pool_pct", 0.0),
                "mar": fp_dca.get("BTCUSDT", {}).get(pk, {}).get("mar_ratio", 0.0),
            },
            "portfolio": {
                "cagr": port_dca.get(pk, {}).get("xirr_pct", 0.0),
                "maxdd": port_dca.get(pk, {}).get("max_dd_pool_pct", 0.0),
                "mar": port_dca.get(pk, {}).get("mar_ratio", 0.0),
            },
        })

    all_plan_defs = [
        ("Lump_Sum", "單筆買進抱著", "被動長抱"),
        ("DCA_Monthly", "定期定額（每月）", "定期定額"),
        ("DCA_Weekly", "定期定額（每週）", "定期定額"),
        ("DCA_Dip_2x", "加碼版定期定額 (2x)", "逢低加碼"),
        ("L4", "策略 L4（主流週線趨勢）", "上線六策略"),
        ("L2", "策略 L2（精簡趨勢跟隨）", "上線六策略"),
        ("L1", "策略 L1（波動調整趨勢）", "上線六策略"),
        ("L3", "策略 L3（雙動能週輪動）", "上線六策略"),
        ("S3", "策略 S3（日線 RSI 回檔）", "上線六策略"),
        ("S2", "策略 S2（通道突破 1D等效）", "上線六策略"),
        ("F4", "對照 F4（週線趨勢 200MA）", "研究對照"),
        ("Hybrid_50DCA_50L4", "混合：50%定額 + 50% L4", "半倉混合"),
        ("Hybrid_50DCA_50L2", "混合：50%定額 + 50% L2", "半倉混合"),
    ]

    all_schemes_rows = []
    for pk, pname, pcat in all_plan_defs:
        all_schemes_rows.append({
            "plan_key": pk,
            "plan_name": pname,
            "category": pcat,
            "twii": {
                "cagr": fp_dca.get("TWII", {}).get(pk, {}).get("xirr_pct", 0.0),
                "maxdd": fp_dca.get("TWII", {}).get(pk, {}).get("max_dd_pool_pct", 0.0),
                "mar": fp_dca.get("TWII", {}).get(pk, {}).get("mar_ratio", 0.0),
            },
            "sp500": {
                "cagr": fp_dca.get("SP500", {}).get(pk, {}).get("xirr_pct", 0.0),
                "maxdd": fp_dca.get("SP500", {}).get(pk, {}).get("max_dd_pool_pct", 0.0),
                "mar": fp_dca.get("SP500", {}).get(pk, {}).get("mar_ratio", 0.0),
            },
            "nas100": {
                "cagr": fp_dca.get("NAS100", {}).get(pk, {}).get("xirr_pct", 0.0),
                "maxdd": fp_dca.get("NAS100", {}).get(pk, {}).get("max_dd_pool_pct", 0.0),
                "mar": fp_dca.get("NAS100", {}).get(pk, {}).get("mar_ratio", 0.0),
            },
            "gold": {
                "cagr": fp_dca.get("XAUUSD", {}).get(pk, {}).get("xirr_pct", 0.0),
                "maxdd": fp_dca.get("XAUUSD", {}).get(pk, {}).get("max_dd_pool_pct", 0.0),
                "mar": fp_dca.get("XAUUSD", {}).get(pk, {}).get("mar_ratio", 0.0),
            },
            "btc": {
                "cagr": fp_dca.get("BTCUSDT", {}).get(pk, {}).get("xirr_pct", 0.0),
                "maxdd": fp_dca.get("BTCUSDT", {}).get(pk, {}).get("max_dd_pool_pct", 0.0),
                "mar": fp_dca.get("BTCUSDT", {}).get(pk, {}).get("mar_ratio", 0.0),
            },
            "portfolio": {
                "cagr": port_dca.get(pk, {}).get("xirr_pct", 0.0),
                "maxdd": port_dca.get(pk, {}).get("max_dd_pool_pct", 0.0),
                "mar": port_dca.get(pk, {}).get("mar_ratio", 0.0),
            },
        })

    rolling_symbols = [
        ("TWII", "台股加權"),
        ("SP500", "S&P 500"),
        ("NAS100", "Nasdaq 100"),
        ("XAUUSD", "黃金"),
        ("BTCUSDT", "比特幣"),
    ]
    rolling_plans = [
        ("Lump_Sum", "單筆買進"),
        ("DCA_Monthly", "每月定額"),
        ("DCA_Dip_2x", "加碼版定額"),
        ("L4", "策略 L4"),
        ("L2", "策略 L2"),
        ("Hybrid_50DCA_50L4", "50/50 混合 L4"),
    ]

    rolling_rows = []
    for sym, sym_name in rolling_symbols:
        p3 = r3_dca.get(sym, {}).get("plans", {})
        p5 = r5_dca.get(sym, {}).get("plans", {})
        for pk, pname in rolling_plans:
            d3 = p3.get(pk, {})
            d5 = p5.get(pk, {})
            rolling_rows.append({
                "symbol": sym,
                "symbol_name": sym_name,
                "plan_key": pk,
                "plan_name": pname,
                "r3_median_cagr": d3.get("median_annualized_return_pct", 0.0),
                "r3_worst_cagr": d3.get("worst_annualized_return_pct", 0.0),
                "r3_median_maxdd": d3.get("median_max_dd_pct", 0.0),
                "r3_worst_maxdd": d3.get("worst_max_dd_pct", 0.0),
                "r3_win_rate": d3.get("positive_return_pct", 0.0),
                "r5_median_cagr": d5.get("median_annualized_return_pct", 0.0),
                "r5_worst_cagr": d5.get("worst_annualized_return_pct", 0.0),
                "r5_median_maxdd": d5.get("median_max_dd_pct", 0.0),
                "r5_worst_maxdd": d5.get("worst_max_dd_pct", 0.0),
                "r5_win_rate": d5.get("positive_return_pct", 0.0),
            })

    article_dca = {
        "id": "dca_compare",
        "slug": "dca-compare",
        "title": "定期定額 vs 單筆買進 vs 六種策略（10 年實證）",
        "subtitle": "台股、S&P 500、那斯達克 100、黃金與比特幣 10 年多標的盲測與 3/5 年滾動實證",
        "takeaway": "只看報酬，定期定額與單筆買進抱著遠勝六種策略；看回撤，策略穩很多。50/50 混合配置把五標的組合最大回撤從約 49% 降到約 16%。",
        "badge": "10 年多標的實證",
        "updated_at": "2026-11",
        "github_report": "https://github.com/diabetepanda/pandatesttrading/blob/main/research/dca_compare/report.md",
        "github_code": "https://github.com/diabetepanda/pandatesttrading/tree/main/research/dca_compare",
        "sections": {
            "takeaway": {
                "title": "一、一句話結論",
                "content": [
                    "只看報酬，定期定額與單筆買進抱著遠勝六種策略；看回撤，策略穩很多。",
                    "如果你買的是長期向上的指數、黃金或比特幣，且能忍受中途帳面跌掉 25%～77%，單筆買進與每月定期定額賺到的總金額與年化報酬（12%～34%）遠勝每筆只敢冒 0.75% 風險的六種交易策略（單一標的年化約 0.8%～2.7%、五標的同池約 2.4%～4.9%）；但若無法忍受資產大幅縮水或長達 2～3 年套牢，策略能把股災回撤縮小到 2%～8%，而「一半資金定期定額 ＋ 一半跑 L4 策略」則是兼顧報酬（年化 9.45%）與抗回撤（回撤從 48.8% 砍到 16.3%）的最實用折衷方案。",
                ],
            },
            "why_low_return": {
                "title": "二、為什麼策略報酬低？",
                "content": [
                    "每筆只冒 0.75% 風險、大部分資金閒置，設計目的是控制回撤，不是跟滿倉長抱比漲幅。",
                    "• 單一標的平均動用名目部位極低：策略每次進場嚴格限制「萬一打到停損最多只損失總資金的 0.75%」。在單一標的上，一年平均僅出手 3～5 次，實際動用資金僅約 5%～25%，高達 75%～95% 的資金全年躺在現金中領 0 息。",
                    "• 犧牲資金利用率換取極致防禦：單一標的跑策略總報酬看似只有個位數，但遇到 2020 年 3 月新冠閃崩與 2022 全年升息熊市時，策略回撤僅 1%～3%，更把比特幣 76.6% 的毀滅性崩盤壓制在 4.9% 以內。",
                    "• 多標的同池才是策略的完整型態：當 5 個標的共用一個資金池輪動時，L4 策略 10 年總報酬達 +60.49%（年化 4.85%），最大回撤僅 -8.22%，MAR 達 0.59。",
                ],
            },
            "core_table": {
                "title": "三、核心總表：五大標的與五標的組合全期間對決（同一期間、同樣總投入 $100,000）",
                "content": [
                    "台股加權（^TWII）、S&P 500、Nasdaq 100、黃金（XAUUSD）皆為 10 年日線（2016-11 ~ 2026-11）；比特幣依專案既有日線快取為 7.1 年（2019-10 ~ 2026-11）。定期定額與混合方案採資金加權年化內部報酬率（XIRR），單筆與策略採 CAGR；最大回撤統一以總資金池淨值曲線計算，MAR = 年化報酬 ÷ 最大回撤。",
                ],
            },
            "rolling_summary": {
                "title": "四、滾動測試重點（持有 3 年與 5 年中位數與最差情況）",
                "content": [
                    "為了消除「剛好挑在 2016 年底起漲點買進」的起點偏誤，我們把歷史上每一個月都當作一個獨立進場起點，測試連續持有 3 年（85 個梯次）與 5 年（61 個梯次，比特幣 26 個梯次）：",
                    "• 持有 3 年：定期定額在台美股與黃金的年化報酬中位數（13.7%～21.3%）與 MAR（0.96～1.24）全面高於單筆買進；若不幸買在最高點（最差情況），單筆買進最大回撤達 26%～77%，定期定額與混合方案展現更好的平滑防禦。",
                    "• 持有 5 年：所有歷史起點月份抱滿 5 年，單筆買進、定期定額、加碼版與 50/50 混合方案正報酬勝率全部達到 100.0%，完全零虧損！",
                ],
            },
            "practical_findings": {
                "title": "五、實用發現",
                "items": [
                    "每週扣與每月扣幾乎沒差：10 年交易次數從 121 次暴增至 522 次，但年化報酬差距在台股、S&P 500、Nasdaq 100 與黃金皆不到 0.08%（比特幣僅差 0.26%）。配合每月發薪日扣一次即可，不需要每週扣增加手續費負擔。",
                    "跌深加碼版每年只多約 0.25%～0.48%：逢收盤價低於 200MA 或回落超過 20% 時當月加倍扣款（DCA_Dip_2x），每年年化報酬多出約 0.25%～0.48%（若採固定十萬預備金池提早打滿，總報酬可多出 26%～61%），但現實中需具備在市場恐慌時連續掏出雙倍現金的心理韌性。",
                    "扣滿 5 年在所有起點都沒有虧損：在 5 年滾動測試中，單筆買進、每月/每週定額、加碼版與 50/50 混合配置在所有 61 個歷史起點月份（比特幣 26 個梯次）正報酬勝率全部達到 100.0%，沒有任何一個起點出現虧損。",
                    "50/50 混合配置把五標的組合最大回撤從約 49% 降到約 16%：一半資金定期定額、一半資金跑 L4 策略，五標的組合總報酬達 +102.5%（年化 XIRR +9.45%），同時把五標的等權單筆買進 -48.8% 的巨額回撤大幅砍至 -16.3%，實現長線複利與心理耐受的最佳平衡。",
                ],
            },
            "limitations": {
                "title": "六、研究限制與免責聲明",
                "content": [
                    "• 比特幣主資料只有約 7.1 年（2019-10 ~ 2026-11，依專案既有日線快取），其餘四大標的為 10 年日線（2016-11 ~ 2026-11）。",
                    "• 過去績效不代表未來；這是研究，不是投資建議。所有內容僅供量化研究與策略回測分析參考，投資人應獨立評估自身財務能力與風險承受度。",
                ],
            },
        },
        "tables": {
            "core_summary": core_summary_rows,
            "all_schemes": all_schemes_rows,
            "rolling_summary": rolling_rows,
        },
        "disclaimer": {
            "text": "本研究僅供策略回測與學術實證參考，過去績效不保證未來表現，非個人化投資建議。",
            "github_links": [
                {
                    "title": "完整 10 年定期定額實證報告 (report.md)",
                    "url": "https://github.com/diabetepanda/pandatesttrading/blob/main/research/dca_compare/report.md",
                },
                {
                    "title": "回測原始程式碼與資料規格 (research/dca_compare)",
                    "url": "https://github.com/diabetepanda/pandatesttrading/tree/main/research/dca_compare",
                },
            ],
        },
    }

    # -------------------------------------------------------------
    # Article 6: 美股與台股槓桿實證：倍率甜蜜點、借貸利率紅線與跌深加碼（20～34 年）
    # -------------------------------------------------------------
    lev_res_path = repo_root / "research" / "leverage_study" / "results.json"
    if lev_res_path.exists():
        with open(lev_res_path, "r", encoding="utf-8") as f:
            lev_res = json.load(f)
    else:
        lev_res = {}

    lev_m1 = lev_res.get("module_1", {})
    lev_m2 = lev_res.get("module_2", {})
    lev_m3 = lev_res.get("module_3", {})
    lev_m4 = lev_res.get("module_4", {})
    lev_m5 = lev_res.get("module_5", {})
    lev_ds = lev_res.get("data_summary", {})

    lev_target_syms = [
        ("SPY", "S&P 500 含息 (SPY)"),
        ("QQQ", "Nasdaq 100 含息 (QQQ)"),
        ("0050.TW", "台股台灣50含息 (0050.TW)"),
        ("^TWII", "台股加權指數 (^TWII 不含息)"),
    ]
    lev_heatmap: dict[str, Any] = {}
    for sym, sym_label in lev_target_syms:
        sym_m1 = lev_m1.get("symbols", {}).get(sym, {})
        lev_heatmap[sym] = {"label": sym_label, "periods": {}}
        for pk in ("full_history", "recent_10y"):
            p_data = sym_m1.get(pk, {})
            lm = p_data.get("lump_sum_matrix", {})
            dm = p_data.get("dca_matrix", {})
            rows = []
            for lv in lev_m1.get("grid_leverages", [1.0, 1.2, 1.3, 1.5, 1.75, 2.0, 2.5, 3.0]):
                lk = f"{lv:.2f}x"
                c0 = lm.get(lk, {}).get("0pct", {})
                c2 = lm.get(lk, {}).get("2pct", {})
                c3 = lm.get(lk, {}).get("3pct", {})
                c5 = lm.get(lk, {}).get("5pct", {})
                c7 = lm.get(lk, {}).get("7pct", {})
                d3 = dm.get(lk, {}).get("3pct", {})
                r10 = c3.get("rolling", {}).get("10y", {})
                rows.append({
                    "lev": lk,
                    "cagr_0pct": c0.get("cagr_pct", 0.0),
                    "cagr_2pct": c2.get("cagr_pct", 0.0),
                    "cagr_3pct": c3.get("cagr_pct", 0.0),
                    "cagr_5pct": c5.get("cagr_pct", 0.0),
                    "cagr_7pct": c7.get("cagr_pct", 0.0),
                    "mdd_3pct": c3.get("mdd_pct", 0.0),
                    "sharpe_3pct": c3.get("sharpe", 0.0),
                    "calmar_3pct": c3.get("calmar", 0.0),
                    "win10y_3pct": r10.get("win_rate_vs_1x_pct"),
                    "p5_10y_3pct": r10.get("p5_cagr_pct"),
                    "dca_xirr_3pct": d3.get("xirr_pct", 0.0),
                })
            lev_heatmap[sym]["periods"][pk] = {
                "start_date": p_data.get("start_date", ""),
                "end_date": p_data.get("end_date", ""),
                "years": p_data.get("years", 0.0),
                "benchmark_1x": p_data.get("benchmark_1x", {}),
                "breakeven_rates_pct": p_data.get("breakeven_rates_pct", {}),
                "breakeven_rates_with_etf_fee_pct": p_data.get("breakeven_rates_with_etf_fee_pct", {}),
                "rows": rows,
            }

    real_etf_pairs = [
        ("SSO", "SPY", "2x S&P 500"),
        ("UPRO", "SPY", "3x S&P 500"),
        ("QLD", "QQQ", "2x Nasdaq 100"),
        ("TQQQ", "QQQ", "3x Nasdaq 100"),
        ("00631L.TW", "0050.TW", "2x 台灣50 (期貨正二)"),
        ("00631L.TW", "^TWII", "2x 台灣50 vs 不含息加權"),
    ]
    real_etf_rows = []
    for etf_s, und_s, desc_s in real_etf_pairs:
        c = lev_m1.get("real_etf_comparison", {}).get(etf_s, {}).get(und_s, {})
        if not c:
            continue
        real_etf_rows.append({
            "etf": etf_s,
            "underlying": und_s,
            "desc": desc_s,
            "start_date": c.get("start_date", ""),
            "end_date": c.get("end_date", ""),
            "years": c.get("actual_etf", {}).get("years", 0.0),
            "und_1x_cagr": c.get("underlying_1x", {}).get("cagr_pct", 0.0),
            "theo_0pct_cagr": c.get("theoretical_0pct_cost", {}).get("cagr_pct", 0.0),
            "actual_cagr": c.get("actual_etf", {}).get("cagr_pct", 0.0),
            "actual_mdd": c.get("actual_etf", {}).get("mdd_pct", 0.0),
            "implied_rate_pct": c.get("implied_annual_financing_rate_pct", 0.0),
            "corr": c.get("daily_return_corr", 0.0),
        })

    # Tools comparison & margin buffer trap from Modules 1-4
    spy_m1_15x = lev_m1.get("symbols", {}).get("SPY", {}).get("full_history", {}).get("lump_sum_matrix", {}).get("1.50x", {}).get("3pct", {})
    tw50_m1_15x = lev_m1.get("symbols", {}).get("0050.TW", {}).get("full_history", {}).get("lump_sum_matrix", {}).get("1.50x", {}).get("3pct", {})
    spy_m2_20x = lev_m2.get("symbols", {}).get("SPY", {}).get("full_history", {}).get("grid", {}).get("2.00x", {}).get("6.0pct", {})
    qqq_m2_20x = lev_m2.get("symbols", {}).get("QQQ", {}).get("full_history", {}).get("grid", {}).get("2.00x", {}).get("6.0pct", {})
    twii_m2_25x = lev_m2.get("symbols", {}).get("^TWII", {}).get("full_history", {}).get("grid", {}).get("2.50x", {}).get("6.5pct", {})
    tw50_m2_25x = lev_m2.get("symbols", {}).get("0050.TW", {}).get("full_history", {}).get("grid", {}).get("2.50x", {}).get("6.5pct", {})
    spy_m3_20y = lev_m3.get("symbols", {}).get("SPY", {}).get("full_history", {}).get("tenors", {}).get("20y_loan", {}).get("by_loan_size", {}).get("borrow_3y_savings", {}).get("by_rate", {}).get("2.5pct", {}).get("lump_sum_loan", {})
    spy_m3_7y = lev_m3.get("symbols", {}).get("SPY", {}).get("full_history", {}).get("tenors", {}).get("7y_loan", {}).get("by_loan_size", {}).get("borrow_3y_savings", {}).get("by_rate", {}).get("3.5pct", {}).get("lump_sum_loan", {})
    tw50_m3_20y = lev_m3.get("symbols", {}).get("0050.TW", {}).get("full_history", {}).get("tenors", {}).get("20y_loan", {}).get("by_loan_size", {}).get("borrow_3y_savings", {}).get("by_rate", {}).get("2.5pct", {}).get("lump_sum_loan", {})
    spy_m4_13x = lev_m4.get("symbols", {}).get("SPY", {}).get("full_history", {}).get("thresholds", {}).get("maint_130pct", {}).get("grid", {}).get("1.30x", {}).get("2.5pct", {})
    spy_m4_20x = lev_m4.get("symbols", {}).get("SPY", {}).get("full_history", {}).get("thresholds", {}).get("maint_130pct", {}).get("grid", {}).get("2.00x", {}).get("2.5pct", {})
    twii_m4_13x = lev_m4.get("symbols", {}).get("^TWII", {}).get("full_history", {}).get("thresholds", {}).get("maint_130pct", {}).get("grid", {}).get("1.30x", {}).get("2.5pct", {})
    tw50_m4_14x = lev_m4.get("symbols", {}).get("0050.TW", {}).get("full_history", {}).get("thresholds", {}).get("maint_130pct", {}).get("grid", {}).get("1.40x", {}).get("2.5pct", {})

    tools_comparison_rows = [
        {
            "tool": "房貸增貸 / 7年低利信貸",
            "rate_range": "房貸 2.2%～2.8% / 信貸 2.5%～3.5%",
            "safe_cap": "借 2～3 年儲蓄額（月付 <50% 月現金流）",
            "mc_rate": "0.0%（無維持率斷頭）",
            "perf_summary": (
                f"SPY 20年房貸@2.5% 10年勝率 {spy_m3_20y.get('rolling', {}).get('10y', {}).get('win_rate_vs_dca_pct', 0.0):.1f}%（中位數淨值多 +{spy_m3_20y.get('rolling', {}).get('10y', {}).get('median_excess_vs_dca_pct', 0.0):.1f}%）；"
                f"7年信貸@3.5% 月付佔比 {spy_m3_7y.get('pmt_to_monthly_cf_pct', 0.0):.1f}%；0050.TW 勝率 {tw50_m3_20y.get('rolling', {}).get('10y', {}).get('win_rate_vs_dca_pct', 0.0):.1f}%"
            ),
            "pros_cons": "優點：有穩定薪資就抱得住股災。缺點：每月需固定還本付息。",
            "rank": "🏆 首選",
            "badge_cls": "badge-ok",
        },
        {
            "tool": "正二槓桿 ETF（配置 50%～100%）",
            "rate_range": "隱含總成本約 2.9%～3.2%/年",
            "safe_cap": "1.3x～1.5x 長抱 / 2.0x 搭年線濾網",
            "mc_rate": "0.0%（每日再平衡，永不追繳）",
            "perf_summary": (
                f"SPY 1.5x@3% 年化 {spy_m1_15x.get('cagr_pct', 0.0):.2f}%（10年勝率 {spy_m1_15x.get('rolling', {}).get('10y', {}).get('win_rate_vs_1x_pct', 0.0):.1f}%）；"
                f"0050.TW 1.5x@3% 年化 {tw50_m1_15x.get('cagr_pct', 0.0):.2f}%（勝率 {tw50_m1_15x.get('rolling', {}).get('10y', {}).get('win_rate_vs_1x_pct', 0.0):.1f}%）"
            ),
            "pros_cons": "優點：免盯盤、無現金流壓力、00631L免股利稅。缺點：震盪盤有波動耗損。",
            "rank": "🏆 首選",
            "badge_cls": "badge-ok",
        },
        {
            "tool": "股票質押（只繳息不還本）",
            "rate_range": "約 2.3%～3.2%/年",
            "safe_cap": "總槓桿 ≤ 1.30x～1.40x（初始維持率 ≥350%）",
            "mc_rate": (
                f"1.3x 斷頭率 {spy_m4_13x.get('rolling', {}).get('10y', {}).get('margin_call_rate_pct', 0.0):.1f}%（SPY/^TWII 皆 0%）；"
                f"開到 2.0x 斷頭率飆至 {spy_m4_20x.get('rolling', {}).get('10y', {}).get('margin_call_rate_pct', 0.0):.1f}%"
            ),
            "perf_summary": (
                f"SPY 1.3x@2.5% 年化 {spy_m4_13x.get('cagr_pct', 0.0):.2f}%（勝率 {spy_m4_13x.get('rolling', {}).get('10y', {}).get('win_rate_vs_1x_pct', 0.0):.1f}%）；"
                f"^TWII 1.3x 斷頭 {twii_m4_13x.get('rolling', {}).get('10y', {}).get('margin_call_rate_pct', 0.0):.1f}%；"
                f"0050.TW 1.4x 年化 {tw50_m4_14x.get('cagr_pct', 0.0):.2f}%"
            ),
            "pros_cons": "優點：只繳息不還本、股息可付息。缺點：借太滿遇 -50% 大崩盤仍會斷頭。",
            "rank": "🥈 次選",
            "badge_cls": "badge-warn",
        },
        {
            "tool": "券商一般融資（Margin）",
            "rate_range": "台股 6.5%～7.0% / 美股 6.0%～8.0%+",
            "safe_cap": "不建議長期持有（>1.4x 即高危）",
            "mc_rate": (
                f"^TWII 2.5x 10年斷頭率 {twii_m2_25x.get('rolling', {}).get('10y', {}).get('margin_call_rate_pct', 0.0):.1f}%；"
                f"SPY 2.0x {spy_m2_20x.get('rolling', {}).get('10y', {}).get('margin_call_rate_pct', 0.0):.1f}%；"
                f"QQQ 2.0x {qqq_m2_20x.get('rolling', {}).get('10y', {}).get('margin_call_rate_pct', 0.0):.1f}%"
            ),
            "perf_summary": (
                f"0050.TW 2.5x 在 2009 後多頭仍有 {tw50_m2_25x.get('rolling', {}).get('5y', {}).get('margin_call_rate_pct', 0.0):.1f}% 5年斷頭率；"
                f"若留備用金防斷頭，SPY 2.0x 實質槓桿剩 {spy_m2_20x.get('peak_crash_effective_leverage', 0.0):.2f}x"
            ),
            "pros_cons": "缺點：利率極高又容易斷頭在阿呆谷，留備用金會把有效槓桿稀釋到不划算。",
            "rank": "💀 不推薦",
            "badge_cls": "badge-danger",
        },
    ]

    margin_buffer_trap = [
        {
            "label": "SPY 2.0x 美股融資 (@6.0%)",
            "entry_dd_mc": spy_m2_20x.get("entry_drawdown_to_margin_call_pct", 0.0),
            "mc_rate_10y": spy_m2_20x.get("rolling", {}).get("10y", {}).get("margin_call_rate_pct", 0.0),
            "req_buf_pct": spy_m2_20x.get("peak_crash_req_buffer_pct", 0.0),
            "eff_lev": spy_m2_20x.get("peak_crash_effective_leverage", 0.0),
            "buf_cagr": spy_m2_20x.get("peak_crash_buffered_cagr_pct", 0.0),
        },
        {
            "label": "QQQ 2.0x 美股融資 (@6.0%)",
            "entry_dd_mc": qqq_m2_20x.get("entry_drawdown_to_margin_call_pct", 0.0),
            "mc_rate_10y": qqq_m2_20x.get("rolling", {}).get("10y", {}).get("margin_call_rate_pct", 0.0),
            "req_buf_pct": qqq_m2_20x.get("peak_crash_req_buffer_pct", 0.0),
            "eff_lev": qqq_m2_20x.get("peak_crash_effective_leverage", 0.0),
            "buf_cagr": qqq_m2_20x.get("peak_crash_buffered_cagr_pct", 0.0),
        },
        {
            "label": "^TWII 2.5x 台股四成融資 (@6.5%)",
            "entry_dd_mc": twii_m2_25x.get("entry_drawdown_to_margin_call_pct", 0.0),
            "mc_rate_10y": twii_m2_25x.get("rolling", {}).get("10y", {}).get("margin_call_rate_pct", 0.0),
            "req_buf_pct": twii_m2_25x.get("peak_crash_req_buffer_pct", 0.0),
            "eff_lev": twii_m2_25x.get("peak_crash_effective_leverage", 0.0),
            "buf_cagr": twii_m2_25x.get("peak_crash_buffered_cagr_pct", 0.0),
        },
    ]

    # Module 5 dynamic leverage & dip buying comparison
    mod5_defs = [
        ("baselines", "baseline_1.0x", "基準：全程 1.0x（滿倉／定投）", "基準"),
        ("baselines", "baseline_fixed_1.5x_r3pct", "對照：全程固定 1.5x（3% 成本）", "固定槓桿"),
        ("e1_cash_reserve_dip", "e1_80_20_staged", "E1：留 20% 現金等大跌分批抄底", "現金等跌"),
        ("e1_cash_reserve_dip", "e1_70_30_staged", "E1：留 30% 現金等大跌分批抄底", "現金等跌"),
        ("e2_leveraged_dip", "e2_dd15_lev1.5x_r2.5pct", "E2：跌 -15% 開 1.5x（回前高降 1x, 2.5%）", "跌深借貸"),
        ("e2_leveraged_dip", "e2_dd20_lev1.5x_r2.5pct", "E2：跌 -20% 開 1.5x（回前高降 1x, 2.5%）", "跌深借貸"),
        ("e2_leveraged_dip", "e2_dd20_lev2.0x_r2.5pct", "E2：跌 -20% 開 2.0x（回前高降 1x, 2.5%）", "跌深借貸"),
        ("e3_sma200_dynamic", "e3_sma200_lev1.5x_buf2pct_r3pct", "E3：200日線 ±2% 緩衝 1.5x / 1.0x (3%)", "年線濾網"),
        ("e3_sma200_dynamic", "e3_sma200_lev2.0x_buf2pct_r3pct", "E3：200日線 ±2% 緩衝 2.0x / 1.0x (3%)", "年線濾網"),
    ]
    lev_dynamic: dict[str, Any] = {}
    for sym, sym_label in lev_target_syms:
        sym_m5 = lev_m5.get("symbols", {}).get(sym, {})
        lev_dynamic[sym] = {"label": sym_label, "periods": {}}
        for pk in ("full_history", "recent_10y"):
            p_data = sym_m5.get(pk, {})
            d_rows = []
            for grp, k, row_label, cat_label in mod5_defs:
                cell = p_data.get(grp, {}).get(k, {})
                lump = cell.get("lump_sum", {})
                dca = cell.get("dca", {})
                r10_l = lump.get("rolling", {}).get("10y", {})
                r10_d = dca.get("rolling", {}).get("10y", {})
                w_dca = r10_d.get("win_rate_vs_1x_dca_pct", r10_d.get("win_rate_vs_1x_pct"))
                note = ""
                if grp == "e1_cash_reserve_dip":
                    note = f"加碼觸發 {lump.get('deploy_triggers_count', 0)} 次"
                elif grp == "e2_leveraged_dip":
                    note = f"啟動 {lump.get('episodes_triggered', 0)} 次 · 130%斷頭 {lump.get('pledge_130_breach_episodes', 0)} 次"
                elif grp == "e3_sma200_dynamic":
                    note = f"年均換倉 {lump.get('switches_per_year', 0.0):.1f} 次 · 開槓桿佔時 {lump.get('pct_time_in_bull_leverage', 0.0):.0f}%"
                else:
                    note = "固定配置對照"
                d_rows.append({
                    "key": k,
                    "label": row_label,
                    "category": cat_label,
                    "lump_cagr": lump.get("cagr_pct", 0.0),
                    "lump_mdd": lump.get("mdd_pct", 0.0),
                    "lump_sharpe": lump.get("sharpe", 0.0),
                    "lump_calmar": lump.get("calmar", 0.0),
                    "lump_win10y": r10_l.get("win_rate_vs_1x_pct"),
                    "dca_xirr": dca.get("xirr_pct", 0.0),
                    "dca_mult": dca.get("final_multiple", 0.0),
                    "dca_win10y": w_dca,
                    "note": note,
                })
            lev_dynamic[sym]["periods"][pk] = {
                "start_date": p_data.get("start_date", ""),
                "end_date": p_data.get("end_date", ""),
                "years": p_data.get("years", 0.0),
                "rows": d_rows,
            }

    spy_e3_15 = lev_m5.get("symbols", {}).get("SPY", {}).get("full_history", {}).get("e3_sma200_dynamic", {}).get("e3_sma200_lev1.5x_buf2pct_r3pct", {})
    spy_e3_lump_win = spy_e3_15.get("lump_sum", {}).get("rolling", {}).get("10y", {}).get("win_rate_vs_1x_pct", 0.0)
    spy_e3_dca_win = spy_e3_15.get("dca", {}).get("rolling", {}).get("10y", {}).get("win_rate_vs_1x_pct", 0.0)

    article_leverage = {
        "id": "leverage_study",
        "slug": "leverage-study",
        "title": "美股與台股槓桿實證：倍率甜蜜點、借貸利率紅線與跌深加碼（20～34 年）",
        "subtitle": "S&P 500、Nasdaq 100 與台股 0050／加權之全歷史含大崩盤與近 10 年雙期間、滾動 5/10/15 年與四大工具實戰比較",
        "takeaway": (
            f"全週期槓桿甜蜜點落在 1.3x～1.6x（正二 ETF 或年線濾網上限 1.5x～2.0x）；利率 ≤3.5% 為黃金區、>5.0% 進禁區；"
            f"房貸增貸／低利信貸與正二 ETF 零斷頭勝出；200 日線 ±2% 緩衝法在 SPY 滾動 10 年勝率達 {spy_e3_lump_win:.1f}%～{spy_e3_dca_win:.1f}%。"
        ),
        "badge": "20～34 年實證",
        "updated_at": "2026-11",
        "github_report": "https://github.com/diabetepanda/pandatesttrading/blob/main/research/leverage_study/report.md",
        "github_code": "https://github.com/diabetepanda/pandatesttrading/tree/main/research/leverage_study",
        "highlight_cards": [
            {
                "label": "全週期甜蜜點",
                "value": "1.3x ～ 1.6x",
                "desc": "正二 ETF 或 200 日線濾網上限可達 1.5x～2.0x；超過 2.0x 遇大崩盤會因波動耗損倒退。",
            },
            {
                "label": "利率黃金區",
                "value": "≤ 3.5%",
                "desc": "4%～5% 為警戒區，超過 5.0% 進禁區（QQQ 2x 全歷史兩平利率僅 2.56%）。",
            },
            {
                "label": "工具首選",
                "value": "房貸增貸／正二",
                "desc": "低利房貸／信貸與正二 ETF 零維持率斷頭；券商 2.5x 融資在台股 10 年斷頭率達 92.7%。",
            },
            {
                "label": "最強動態法",
                "value": "200 日線 ±2%",
                "desc": f"SPY 全歷史 1.5x 單筆與定投滾動 10 年勝率高達 {spy_e3_lump_win:.1f}% ～ {spy_e3_dca_win:.1f}%，回撤大幅壓低。",
            },
        ],
        "tables": {
            "heatmap": lev_heatmap,
            "real_etfs": real_etf_rows,
            "tools_comparison": tools_comparison_rows,
            "margin_buffer_trap": margin_buffer_trap,
            "dynamic_comparison": lev_dynamic,
        },
        "sections": {
            "safety_rules": {
                "title": "四、給一般投資人的安全界線",
                "items": [
                    "利率紅線：年利率 ≤3.5%（如台灣房貸增貸 2.2%～2.8%、低利質押／信貸 2.5%～3.2%、台股正二隱含成本 2.92%）屬於黃金區；3.5%～5.0% 需搭定期定額或年線濾網；>5.0%（如券商融資 6%～7%）在完整週期中不該借。",
                    "信貸月付金上限：7 年期個人信貸本息均攤，建議借款上限為「2～3 年儲蓄額」，月付金嚴守「< 月可支配儲蓄的 50%」（3 年儲蓄額在 2.5%～3.5% 利率下佔 46.8%～48.4%），切勿拖到 24 個月才慢慢買，避免資金空轉白繳息。",
                    "股票質押安全上限：若以能挺過 2000 / 2008 年級別大崩盤（-55%～-66%）為標準，總槓桿應嚴格控制在 ≤ 1.30x～1.40x（初始維持率 350%～433% 以上，即每 100 萬現股最多借 30～40 萬），此時歷史滾動 10 年斷頭率趨近 0%。",
                    "最容易爆倉排行：第 1 名台股 2.5x 券商融資（跌 22% 即斷頭，^TWII 滾動 10 年斷頭率 92.7%）；第 2 名美股 2.0x 融資／質押借滿 2.0x（10 年斷頭率 40%～50%）；第 3 名單筆重倉 3 倍 ETF（TQQQ/UPRO）遇網路泡沫級崩盤淨值蒸發 99%。",
                ],
            },
            "limitations": {
                "title": "五、研究限制與資料校正誠實揭露",
                "content": [
                    f"1. 台股含息與不含息期間差異：^TWII（價格指數不含息）抓取期間為 {lev_ds.get('^TWII', {}).get('start_date', '1997-07-02')} ～ {lev_ds.get('^TWII', {}).get('end_date', '2026-11-24')}（{lev_ds.get('^TWII', {}).get('years', 29.4):.2f} 年），完整涵蓋 2000 年網路泡沫（-66.22%）與 2008 年金融海嘯；而 Yahoo Finance 上 0050.TW 含息還原有效日線始於 {lev_ds.get('0050.TW', {}).get('start_date', '2009-03-10')}（{lev_ds.get('0050.TW', {}).get('years', 17.71):.2f} 年），期間最大回撤僅 -33.83%，未涵蓋 2000/2008 兩次腰斬。因此台股「殖利率付息能力」看 0050.TW（股息年貢獻 +3.52%），但「極端維持率斷頭壓力」必須以 ^TWII 的 29.4 年數據為準。",
                    "2. Yahoo Finance 台灣 ETF 分割與配息斷層修正：0050.TW 於 2025-06-11 進行 1 拆 4 分割，原始資料僅回溯至 2014-01-02 並遺漏 2009–2014 調整係數，本研究已於回測腳本中修正 2013-12-31 前價格 ÷4、補齊歷年官方配息還原並剔除 2009 年初異常錯價；00631L.TW 於 2026-03-25 進行 1 拆 22 分割且 2015-08-24 前存在錯價，故取完整無斷層區間 2015-08-25 ～ 2026-11-24（11.25 年，與大盤日報酬相關係數 0.9766）進行實證對照。",
                    "3. 倖存者偏差與利率環境變遷：美股與台股過去 30 年受惠於科技與半導體紅利屬於全球最強市場，若套用於長期盤整市場（如 1990–2020 日經）開槓桿將會虧損；且 2009–2021 處於罕見低利率環境，高利率時期應改對照熱力矩陣中的 5%～7% 欄位。",
                ],
            },
        },
        "disclaimer": {
            "text": "本研究僅供量化回測與歷史實證參考，過去績效不代表未來表現，亦不構成個人化投資或借貸建議。",
            "github_links": [
                {
                    "title": "完整槓桿實證研究報告 (report.md)",
                    "url": "https://github.com/diabetepanda/pandatesttrading/blob/main/research/leverage_study/report.md",
                },
                {
                    "title": "回測主程式與可重現腳本 (research/leverage_study)",
                    "url": "https://github.com/diabetepanda/pandatesttrading/tree/main/research/leverage_study",
                },
            ],
        },
    }

    # -------------------------------------------------------------
    # Article 7: 每月 1 萬台幣定期定額：台美股與槓桿 ETF 全組合最佳配置（約 27～29 年）
    # -------------------------------------------------------------
    grid_res_path = repo_root / "research" / "dca_allocation_grid" / "results.json"
    if grid_res_path.exists():
        with open(grid_res_path, "r", encoding="utf-8") as f:
            grid_res = json.load(f)
    else:
        grid_res = {}

    def _fmt_weights(weights: dict) -> str:
        order = ["0050", "00631L", "SPY", "QQQ", "SSO", "QLD", "UPRO", "TQQQ"]
        parts = []
        for sym in order:
            w = weights.get(sym, 0)
            if w:
                parts.append(f"{sym} {int(w)}%")
        for sym, w in weights.items():
            if sym not in order and w:
                parts.append(f"{sym} {int(w)}%")
        return " + ".join(parts)

    def _fmt_rebalance(mode: str) -> str:
        return {"none": "不做", "annual": "每年", "quarterly": "每季"}.get(mode, mode or "—")

    def _fmt_filter(mode: str) -> str:
        return "開啟" if mode == "sma200_buf2" else "不加"

    def _fmt_twd_wan(val: float | None) -> str:
        if val is None:
            return "—"
        if abs(val) >= 1e8:
            yi = int(val // 1e8)
            rem_wan = round((val - yi * 1e8) / 1e4)
            return f"{yi} 億 {rem_wan:,} 萬"
        wan = val / 1e4
        if abs(wan) >= 1000:
            return f"{round(wan):,} 萬"
        return f"{wan:,.1f} 萬"

    def _fmt_recovery(item: dict) -> str:
        rec = item.get("recovery_months")
        if isinstance(rec, (int, float)):
            return f"{rec:.1f} 個月"
        return str(rec) if rec is not None else "—"

    def _extract_grid_entry(item: dict, rank: int) -> dict:
        r5 = item.get("rolling", {}).get("5y", {})
        r10 = item.get("rolling", {}).get("10y", {})
        return {
            "rank": rank,
            "config": _fmt_weights(item.get("weights", {})),
            "xirr_pct": item.get("xirr"),
            "mdd_pct": item.get("max_drawdown_pct"),
            "mdd_twd": item.get("max_drawdown_twd"),
            "recovery": _fmt_recovery(item),
            "mar": item.get("mar"),
            "sharpe": item.get("sharpe"),
            "effective_leverage": item.get("effective_leverage"),
            "rebalance": _fmt_rebalance(item.get("rebalance", "")),
            "filter_200ma": _fmt_filter(item.get("filter", "")),
            "rolling_10y_status": r10.get("status", "資料不足"),
            "rolling_10y_win_pct": r10.get("win_rate_pct"),
            "rolling_10y_p5_pct": r10.get("p5_xirr_pct"),
            "rolling_5y_status": r5.get("status", "資料不足"),
            "rolling_5y_win_pct": r5.get("win_rate_pct"),
            "rolling_5y_p5_pct": r5.get("p5_xirr_pct"),
            "final_value_twd": item.get("final_value_twd"),
            "final_value_label": _fmt_twd_wan(item.get("final_value_twd")),
        }

    grid_groups = grid_res.get("groups", {})
    g_a_full = grid_groups.get("A", {}).get("full_history", {})
    g_b_full = grid_groups.get("B", {}).get("full_history", {})
    g_a_10y = grid_groups.get("A", {}).get("recent_10y", {})
    g_b_10y = grid_groups.get("B", {}).get("recent_10y", {})

    a_full_low = g_a_full.get("summary_stats", {}).get("lowest_drawdown_portfolio", {})
    b_full_low = g_b_full.get("summary_stats", {}).get("lowest_drawdown_portfolio", {})
    a_full_low_mdd = a_full_low.get("max_drawdown_pct", -53.76)
    b_full_low_mdd = b_full_low.get("max_drawdown_pct", -44.49)
    a_full_low_cfg = _fmt_weights(a_full_low.get("weights", {"0050": 100}))
    b_full_low_cfg = _fmt_weights(b_full_low.get("weights", {"SPY": 80, "QQQ": 20}))

    def _build_cap_summary_rows(period_key: str) -> list[dict]:
        rows = []
        for cap_key in ("-30", "-40", "-50", "-60"):
            cap_label = f"{cap_key}%"
            for grp_key, grp_label in (("A", "A 群（只買台股）"), ("B", "B 群（台美股）")):
                g_sub = grid_groups.get(grp_key, {}).get(period_key, {})
                cap_list = g_sub.get("top_by_drawdown_cap", {}).get(cap_key, [])
                if not cap_list:
                    if grp_key == "A":
                        reason = f"純台股全組合最小回撤為 {a_full_low_cfg} 的 {a_full_low_mdd:.2f}%，無法壓進 {cap_label} 以內"
                    else:
                        reason = f"台美股全組合最小回撤為 {b_full_low_cfg}（每季再平衡）的 {b_full_low_mdd:.2f}%，2008 海嘯期間無法壓進 {cap_label} 以內"
                    rows.append({
                        "cap": cap_label,
                        "group": grp_key,
                        "group_label": grp_label,
                        "qualified_label": "0 組符合",
                        "has_match": False,
                        "zero_reason": reason,
                    })
                else:
                    top1 = _extract_grid_entry(cap_list[0], 1)
                    q_label = f"{len(cap_list)} 組符合" if len(cap_list) < 5 else "≥ 5 組（取前 5 名）"
                    rows.append({
                        "cap": cap_label,
                        "group": grp_key,
                        "group_label": grp_label,
                        "qualified_label": q_label,
                        "has_match": True,
                        **top1,
                    })
        return rows

    full_history_summary_rows = _build_cap_summary_rows("full_history")
    recent_10y_summary_rows = _build_cap_summary_rows("recent_10y")

    def _build_top5_groups(period_key: str) -> list[dict]:
        blocks = []
        for cap_key in ("-30", "-40", "-50", "-60"):
            for grp_key, grp_label in (("A", "A 群（只買台股）"), ("B", "B 群（台美股）")):
                cap_list = grid_groups.get(grp_key, {}).get(period_key, {}).get("top_by_drawdown_cap", {}).get(cap_key, [])
                if not cap_list:
                    continue
                blocks.append({
                    "title": f"{cap_key}% 上限 · {grp_label}（列出前 {len(cap_list)} 名）",
                    "cap": f"{cap_key}%",
                    "group": grp_key,
                    "rows": [_extract_grid_entry(item, idx + 1) for idx, item in enumerate(cap_list)],
                })
        return blocks

    full_history_top5_blocks = _build_top5_groups("full_history")
    recent_10y_top5_blocks = _build_top5_groups("recent_10y")

    # Extreme crash comparison (A & B full_history extreme)
    ext_a = g_a_full.get("extreme", {})
    ext_b = g_b_full.get("extreme", {})
    extreme_crashes_rows = []
    for grp_key, grp_label, ext in (
        ("B", "B 群（台美股）極限冠軍", ext_b),
        ("A", "A 群（只買台股）極限冠軍", ext_a),
    ):
        if not ext:
            continue
        crashes = ext.get("crash_drawdowns_pct", {})
        extreme_crashes_rows.append({
            "group": grp_key,
            "group_label": grp_label,
            "config": _fmt_weights(ext.get("weights", {})),
            "effective_leverage": ext.get("effective_leverage"),
            "rebalance": _fmt_rebalance(ext.get("rebalance", "")),
            "filter_200ma": _fmt_filter(ext.get("filter", "")),
            "total_invested_twd": ext.get("total_invested_twd"),
            "total_invested_label": _fmt_twd_wan(ext.get("total_invested_twd")),
            "final_value_twd": ext.get("final_value_twd"),
            "final_value_label": _fmt_twd_wan(ext.get("final_value_twd")),
            "xirr_pct": ext.get("xirr"),
            "mdd_pct": ext.get("max_drawdown_pct"),
            "recovery": _fmt_recovery(ext),
            "crash_2000": crashes.get("2000_dotcom"),
            "crash_2008": crashes.get("2008_gfc"),
            "crash_2020": crashes.get("2020_covid"),
            "crash_2022": crashes.get("2022_rates"),
        })

    # Extract key champions for highlight cards directly from results.json
    b_full_50_1 = (g_b_full.get("top_by_drawdown_cap", {}).get("-50") or [{}])[0]
    a_full_60_1 = (g_a_full.get("top_by_drawdown_cap", {}).get("-60") or [{}])[0]
    b_full_60_1 = (g_b_full.get("top_by_drawdown_cap", {}).get("-60") or [{}])[0]
    b_10y_40_1 = (g_b_10y.get("top_by_drawdown_cap", {}).get("-40") or [{}])[0]
    b_10y_40_sens = b_10y_40_1.get("sensitivity_min_fee_15usd", {})
    b_10y_40_loss_twd = (b_10y_40_1.get("final_value_twd", 0.0) - b_10y_40_sens.get("final_value_twd", 0.0))

    total_runs = grid_res.get("meta", {}).get("grid_counts", {}).get("total_backtest_runs", 233508)
    b_full_50_xirr = b_full_50_1.get("xirr", 15.49)
    b_full_60_xirr = b_full_60_1.get("xirr", 18.18)
    a_full_60_xirr = a_full_60_1.get("xirr", 15.27)
    ext_b_mdd = ext_b.get("max_drawdown_pct", -87.72)
    ext_a_mdd = ext_a.get("max_drawdown_pct", -82.46)
    b_10y_40_xirr_diff = b_10y_40_sens.get("xirr_diff", -2.39)

    article_dca_grid = {
        "id": "dca_allocation_grid",
        "slug": "dca-allocation-grid",
        "title": "每月 1 萬台幣定期定額：台美股與槓桿 ETF 全組合最佳配置（約 27～29 年）",
        "subtitle": (
            f"8 檔台美股與 1x～3x 槓桿 ETF、{total_runs:,} 組全網格回測（全歷史與近 10 年雙期間、再平衡、200 日線濾網與 15 美元低消敏感度）"
        ),
        "takeaway": (
            f"長期全股票定期定額很難把最大回撤壓在 -45% 以內（全歷史最低為 {b_full_low_mdd:.2f}%）；"
            f"能承受 -50%～-60% 回撤的人，小幅槓桿（1.2x～1.4x）就能把全歷史年化報酬明顯拉高至 {b_full_50_xirr:.2f}%～{b_full_60_xirr:.2f}%；"
            f"極限槓桿雖然期末金額最高（{_fmt_twd_wan(ext_b.get('final_value_twd'))}），但中途回撤超過 -80%（{ext_a_mdd:.2f}%～{ext_b_mdd:.2f}%），現實中很難撐住。"
        ),
        "badge": "23 萬組回測",
        "updated_at": "2026-11",
        "github_report": "https://github.com/diabetepanda/pandatesttrading/blob/main/research/dca_allocation_grid/report.md",
        "github_code": "https://github.com/diabetepanda/pandatesttrading/tree/main/research/dca_allocation_grid",
        "highlight_cards": [
            {
                "label": "全歷史 -50% 上限冠軍（B 群）",
                "value": f"XIRR {b_full_50_xirr:.2f}%（{b_full_50_1.get('effective_leverage', 1.2):.1f}x）",
                "desc": (
                    f"{_fmt_weights(b_full_50_1.get('weights', {}))}（{_fmt_rebalance(b_full_50_1.get('rebalance', 'none'))}再平衡、{_fmt_filter(b_full_50_1.get('filter', ''))} 200 日線濾網），"
                    f"MDD {b_full_50_1.get('max_drawdown_pct', -49.81):.2f}%、期末 {_fmt_twd_wan(b_full_50_1.get('final_value_twd'))}，滾動 10 年勝率 {b_full_50_1.get('rolling', {}).get('10y', {}).get('win_rate_pct', 97.65):.2f}%。"
                ),
            },
            {
                "label": "全歷史 -60% 上限冠軍（A / B 群）",
                "value": f"A {a_full_60_xirr:.2f}% ｜ B {b_full_60_xirr:.2f}%",
                "desc": (
                    f"A 群只買台股：{_fmt_weights(a_full_60_1.get('weights', {}))}（{a_full_60_1.get('effective_leverage', 1.3):.1f}x、MDD {a_full_60_1.get('max_drawdown_pct', -58.63):.2f}%、期末 {_fmt_twd_wan(a_full_60_1.get('final_value_twd'))}）；"
                    f"B 群台美混搭：{_fmt_weights(b_full_60_1.get('weights', {}))}（{b_full_60_1.get('effective_leverage', 1.4):.1f}x、年平衡、MDD {b_full_60_1.get('max_drawdown_pct', -59.77):.2f}%、期末 {_fmt_twd_wan(b_full_60_1.get('final_value_twd'))}）。"
                ),
            },
            {
                "label": "極限不歸零配置（20% 00631L + 80% TQQQ）",
                "value": _fmt_twd_wan(ext_b.get("final_value_twd")),
                "desc": (
                    f"有效槓桿 {ext_b.get('effective_leverage', 2.8):.1f}x（{_fmt_rebalance(ext_b.get('rebalance', ''))}再平衡＋{_fmt_filter(ext_b.get('filter', ''))} 200 日線濾網），"
                    f"XIRR {ext_b.get('xirr', 25.27):.2f}%，但最大回撤高達 {ext_b_mdd:.2f}%、谷底回本需 {_fmt_recovery(ext_b)}，現實中極難抱住。"
                ),
            },
            {
                "label": "15 美元低消的影響（近 10 年 -40% 冠軍）",
                "value": f"每年少 {abs(b_10y_40_xirr_diff):.2f}%",
                "desc": (
                    f"近 10 年 B 群 -40% 冠軍（{_fmt_weights(b_10y_40_1.get('weights', {}))}）若用傳統每筆低消 15 美元複委託，"
                    f"XIRR 由 {b_10y_40_1.get('xirr', 34.46):.2f}% 降至 {b_10y_40_sens.get('xirr', 32.07):.2f}%，10 年期末少賺 {_fmt_twd_wan(b_10y_40_loss_twd)}。"
                ),
            },
        ],
        "charts": [
            {
                "title": "全歷史效率前緣圖（1997～2026 / 1999～2026）",
                "src": "./assets/research/efficient_frontier_full_history.png",
                "caption": "深藍線（B 群：台美股混搭）整條位在橘紅線（A 群：只買台股）左上方，代表相同回撤下台美分散能換到更高的年化報酬（如 -60% 內 B 群 18.18% vs A 群 15.27%）。",
            },
            {
                "title": "近 10 年效率前緣圖（2016/12～2026/11）",
                "src": "./assets/research/efficient_frontier_recent_10y.png",
                "caption": "近 10 年受惠台積電與 AI 狂潮，台股橘紅線緊貼台美深藍線；但在低回撤區間（-24%～-40%），加入 20%～40% 美股搭配每季再平衡仍能把回撤再往下壓。",
            },
        ],
        "tables": {
            "full_history_summary": full_history_summary_rows,
            "recent_10y_summary": recent_10y_summary_rows,
            "extreme_crashes": extreme_crashes_rows,
            "full_history_top5": full_history_top5_blocks,
            "recent_10y_top5": recent_10y_top5_blocks,
        },
        "sections": {
            "setup": {
                "title": "一、研究設定（每月 1 萬台幣，怎麼扣、算哪些成本？）",
                "content": [
                    "這份研究模擬一般上班族每月第一個交易日固定投入 10,000 元台幣（不擇時、不借錢、不留現金），把台股 2 檔（0050、00631L）與美股 6 檔（SPY、QQQ、SSO、QLD、UPRO、TQQQ）以每 10% 為一格做全組合網格搜尋，搭配 3 種再平衡（不做／每年／每季）與 2 種濾網（不加／200 日線濾網），總共跑完 233,508 組回測（全歷史與近 10 年雙期間）。",
                    "• 兩大群組與期間：A 群（只用台股 0050、00631L，全歷史 1997-07-02 ～ 2026-11-25，共 353 個月、投入 353 萬）；B 群（台美股 8 檔全開，全歷史 1999-03-10 ～ 2026-11-25，共 333 個月、投入 333 萬）；兩群另同步測試近 10 年（2016-12-01 ～ 2026-11-25，120 個月、投入 120 萬）。",
                    "• 真實交易成本與稅負：美股買賣計算換匯手續費 0.1%、複委託手續費 0.1%（主回測假設無低消，另對前段班重跑每筆低消 15 美元敏感度測試）、美股配息預扣 30% 股息稅；台股定期定額買進手續費以 0% 模擬（貼近券商 1 元優惠），但再平衡買賣時計入 0.1425% 手續費與賣出 0.1% ETF 證交稅。最終資產一律依當日美元兌台幣匯率（TWD=X）換算回台幣。",
                    "• 200 日線 ±2% 濾網規則：當大盤收盤跌破 200 日均線 -2% 時，之後每個月新扣的 1 萬元原本要買槓桿 ETF 的部分，暫時改買對應的 1 倍原型 ETF（00631L→0050、SSO/UPRO→SPY、QLD/TQQQ→QQQ），等站回 200 日均線 +2% 再恢復買槓桿 ETF。這個濾網只改變新資金的去向，完全不賣出原本手上的舊部位，避免熊市殺低與頻繁交易摩擦。",
                ],
            },
            "conclusions": {
                "title": "二、主要結論（從 23 萬組回測看到的四件事）",
                "items": [
                    f"純股票長期幾乎不可能把最大回撤壓在 -45% 以內：在涵蓋 2000 網路泡沫與 2008 金融海嘯的全歷史裡，就算完全不開槓桿，100% 買 0050 最大回撤仍有 {a_full_low_mdd:.2f}%，台美股全組合最小回撤也要 {b_full_low_mdd:.2f}%（{b_full_low_cfg} 每季再平衡）。所以全歷史 -30% 與 -40% 上限都是 0 組符合；近 10 年能出現 -30% 內高報酬，純粹是因為沒遇到 2008 等級的大崩盤。",
                    f"能承受 -50%～-60% 回撤，小幅槓桿（1.2x～1.4x）性價比最高：B 群 -50% 上限冠軍（1.2x、開 200 日線濾網）全歷史 XIRR 達 {b_full_50_xirr:.2f}%、期末 {_fmt_twd_wan(b_full_50_1.get('final_value_twd'))}；-60% 上限冠軍（40% 0050 + 40% QQQ + 20% TQQQ，1.4x、年平衡）更衝上 {b_full_60_xirr:.2f}%、期末 {_fmt_twd_wan(b_full_60_1.get('final_value_twd'))}，且滾動 10 年正報酬勝率高達 97.65%（最差 5% 梯次仍有 +3.75%/年）。只買台股的 A 群用 70% 0050 + 30% 00631L（1.3x）也能在 -60% 內拿到 {a_full_60_xirr:.2f}%（期末 {_fmt_twd_wan(a_full_60_1.get('final_value_twd'))}）。",
                    f"極限槓桿帳面最驚人，但現實中很難撐過兩次 -80% 崩盤：不限回撤下沒有任何一組歸零，B 群極限冠軍（20% 00631L + 80% TQQQ，2.8x）333 萬本金滾成 {_fmt_twd_wan(ext_b.get('final_value_twd'))}，A 群極限冠軍（100% 00631L，2.0x）滾成 {_fmt_twd_wan(ext_a.get('final_value_twd'))}。但代價是 2000 與 2008 兩度蒸發約八成以上（{ext_a_mdd:.2f}%～{ext_b_mdd:.2f}%），谷底回本要熬 3.2～4.9 年，多數人在中途就會崩潰停扣。",
                    f"每月 1 萬元千萬不能踩「15 美元低消」地雷：若把 1 萬元拆成 10%（1,000 元台幣）去買美股卻用每筆低消 15 美元（約 480 台幣）的傳統複委託，等於還沒開始投資就先被吃掉將近一半本金。近 10 年 -40% 冠軍一旦加上 15 美元低消，年化報酬直接少掉 {abs(b_10y_40_xirr_diff):.2f}%（10 年少賺 {_fmt_twd_wan(b_10y_40_loss_twd)}）。小額定期定額美股務必選無低消方案，或集中成 1 檔／累積一季再買。",
                ],
            },
            "how_to_use": {
                "title": "三、怎麼用這份結果（給一般投資人的實戰步驟）",
                "items": [
                    "第一步：先誠實決定自己能承受多少帳面回撤，而不是先看報酬率。如果帳面跌超過 -30% 就睡不著，全股票配置本身就不適合你（需要搭配債券或現金）；能承受 -50%～-60% 波動，再考慮 1.0x～1.4x 的溫和槓桿配置。",
                    "第二步：看「大方向與槓桿區間」，不要死守第 1 名的精確比例。從下方前 5 名清單可以看到，同一個回撤上限裡第 1 名到第 5 名的年化報酬往往只差 0.1%～0.3%，重點在於「台美股分散＋1.2x～1.4x 溫和槓桿＋每年或每季再平衡」，把 3 倍 ETF 最多控制在 10%～20% 衛星部位（或直接改用 2 倍 ETF 00631L / QLD 取代），效果就很接近。",
                    "第三步：檢視工具成本與操作紀律。有美股免低消定期定額管道再用台美混搭；只用台股證券戶的人，70% 0050 + 30% 00631L（或 50/50 每年再平衡）就已經非常簡單好用（註：台灣 00631L 無法設定券商自動定期定額，需每月手動買零股並先開信用戶簽署風險預告書）。",
                ],
            },
            "limitations": {
                "title": "四、研究限制（讀完這些再做決定）",
                "items": [
                    "上市前模擬資料誤差：00631L 在 2015-08-25 以前、0050 在 2009-03-10 以前（以 ^TWII 加計每年 3.0% 估計股息率回溯），以及 SSO／QLD（2006-06 前）、UPRO（2009-06 前）、TQQQ（2010-02 前）上市以前都是模擬資料。真實上市後重疊期比對顯示，模擬版年化報酬比真實 ETF 略高約 0.77%～2.37%（主因真實交換合約利差與台指期收盤時間差），早期數據偏微幅樂觀。",
                    "事後挑選的過度擬合風險：表中的第 1 名是從 233,508 組參數裡事後挑出來的最佳解，必然帶有歷史運氣成分（Overfitting），未來不可能剛好由同一組比例繼續奪冠，因此只應參考區間規律而非死抄單一配方。",
                    "近 10 年剛好是大多頭：2016/12～2026/11 受惠台積電、AI 與美股科技長牛，沒有經歷 2000 或 2008 級別的長期熊市，年化 30%～40% 與較淺的回撤絕對不能當成未來常態。",
                    "主要結果假設沒有 15 美元低消：主回測的美股手續費純按 0.1% 計算、無最低消費限制；若你的券商有單筆低消，小額拆買多檔美股會大幅拉低真實報酬。",
                    "Yahoo 匯率錯價與早期延用：Yahoo Finance 的 TWD=X 匯率原始資料存在小數點錯位（如 2011-10-25、2014-12-31）與單日異常跳點，已在回測前清洗修正；此外 TWD=X 最早資料僅到 2004-03-24（32.0230），更早期間往前延用該匯率作為近似值。",
                    "單一歷史路徑限制：這份研究只回測了過去一條已經發生的歷史路徑（且台美股剛好是近 30 年全球最強市場，具有倖存者偏差），尚未針對未來「長期上漲、長期盤整、持續下跌」等不同假設情境做蒙地卡羅或壓力測試。",
                ],
            },
        },
        "disclaimer": {
            "text": "本研究僅供量化回測與歷史實證參考，過去績效不代表未來表現，亦不構成任何個人化投資建議。",
            "github_links": [
                {
                    "title": "完整 23 萬組定期定額研究報告 (report.md)",
                    "url": "https://github.com/diabetepanda/pandatesttrading/blob/main/research/dca_allocation_grid/report.md",
                },
                {
                    "title": "回測原始程式碼與資料 (research/dca_allocation_grid)",
                    "url": "https://github.com/diabetepanda/pandatesttrading/tree/main/research/dca_allocation_grid",
                },
            ],
        },
    }

    # -------------------------------------------------------------
    # Article 8: 普通人該怎麼投資：從歷史高點回本到八關壓力測試
    # -------------------------------------------------------------
    pr_res_path = repo_root / "research" / "peak_recovery" / "results.json"
    rc_res_path = repo_root / "research" / "robustness_check" / "results.json"
    pr_res = json.loads(pr_res_path.read_text(encoding="utf-8")) if pr_res_path.exists() else {}
    rc_res = json.loads(rc_res_path.read_text(encoding="utf-8")) if rc_res_path.exists() else {}

    rc_g3 = rc_res.get("gate_3_intl_crashes", {})
    rc_g5 = rc_res.get("gate_5_behavior", {}).get("summary_by_strategy", {})
    rc_g6 = rc_res.get("gate_6_bootstrap", {})
    rc_g7 = rc_res.get("gate_7_sensitivity", {}).get("perturbations", {})
    rc_g8 = rc_res.get("gate_8_life_stage", {})
    rc_verdicts = rc_res.get("verdicts", {})

    def _find_pert(group_key: str, substr: str) -> dict:
        for item in rc_g7.get(group_key, []):
            if substr in str(item.get("variant", "")):
                return item
        return {}

    r1_annual = _find_pert("R1", "每年再平衡")
    r1_base = _find_pert("R1", "基準")
    r2_base = _find_pert("R2", "基準")
    r2_5050 = _find_pert("R2", "50% 0050 + 50% 短債")
    r2_7030 = _find_pert("R2", "70% 0050")
    r2_norebal = _find_pert("R2", "不再平衡")
    r3a_base = _find_pert("R3_M3a", "基準")
    r3a_8020 = _find_pert("R3_M3a", "80% 0050 + 20% 00631L")
    r3a_nofilter = _find_pert("R3_M3a", "關閉濾網")
    r3b_base = _find_pert("R3_M3b", "基準")
    r3b_norebal = _find_pert("R3_M3b", "不再平衡")

    r4_gates = rc_verdicts.get("by_recommendation", {}).get("R4", {}).get("gates", {})
    r4_probs = [
        float(v["prob_2x_mdd_lt_50pct"])
        for v in r4_gates.values()
        if isinstance(v, dict) and v.get("prob_2x_mdd_lt_50pct") is not None
    ]
    r4_prob_min = min(r4_probs) if r4_probs else 69.93
    r4_prob_max = max(r4_probs) if r4_probs else 100.0

    # Build verdict summary rows directly from robustness_check/results.json
    verdict_label_map = {
        "R1": "R1 一般人（50% 0050 + 50% SPY，承受 −50%）",
        "R2": "R2 保守族（60% 0050 + 40% 短債，承受 −30%）",
        "R3": "R3 積極族（M3b 1.4x / M3a 1.3x，承受 −60%）",
        "R4": "R4 高槓桿戒律（2 倍以上槓桿不建議）",
        "R5": "R5 熊市回本預期（0050 含息中位數約 20 個月）",
    }
    verdict_rows = []
    for row in rc_verdicts.get("summary_table", []):
        rid = row.get("recommendation_id", "")
        verdict_rows.append({
            "id": rid,
            "label": verdict_label_map.get(rid, rid),
            "gate_1": row.get("gate_1", "—"),
            "gate_2": row.get("gate_2", "—"),
            "gate_3": row.get("gate_3", "—"),
            "gate_4": row.get("gate_4", "—"),
            "gate_5": row.get("gate_5", "—"),
            "gate_6": row.get("gate_6", "—"),
            "gate_7": row.get("gate_7", "—"),
            "gate_8": row.get("gate_8", "—"),
            "overall_verdict": row.get("overall_verdict", "—"),
        })

    # Retirement withdrawal table from gate_8_life_stage.withdrawal_30y
    w30_ports = rc_g8.get("withdrawal_30y", {}).get("portfolios", {})
    w30_order = [
        ("30_70", "30/70 股債（30% 台美股 + 70% 短債）", "撐 23.4 年（83.4 歲）", "前 20 年極安全，但七成短債在低利率下抗通膨不足，82 歲後易把老本啃光。"),
        ("40_60", "40/60 股債（40% 台美股 + 60% 短債）", "撐 21.0 年（81.0 歲）", "前 19 年 100% 存活，穩健平衡，適合極怕波動的高齡退休族。"),
        ("R2", "60/40 股債（R2，60% 股 + 40% 短債）", "撐 16.8 年", "全場破產率最低的甜蜜點：四成短債擋住前十年大跌，六成股票打贏後二十年通膨。"),
        ("R1", "50/50 純股（R1，台美各半）", "撐 11.6 年", "全股票遇到剛退休大跌，邊跌邊賣失血快，72.4% 機率回撤破 −50%。"),
        ("R3_M3a", "1.3x 小槓桿（M3a，70/30 + 200MA 濾網）", "撐 9.5 年", "波動放大，早年遇熊市邊跌邊賣失血過快。"),
        ("R3_M3b", "1.4x 小槓桿（M3b，40/40/20 年平衡）", "撐 8.8 年（68.8 歲）", "剛退休遇大跌超過三分之一機率破產，絕不可帶進退休期。"),
        ("R4_M4", "2.0x 槓桿（正二對照）", "撐 5.6 年（65.6 歲）", "60 歲退休遇日股崩盤，65 歲半就身無分文。"),
    ]
    withdrawal_rows = []
    for pkey, plabel, jp_note, comment in w30_order:
        p = w30_ports.get(pkey, {})
        all_p = p.get("all_paths", {})
        early_p = p.get("early_crash_paths", {})
        if not all_p:
            continue
        withdrawal_rows.append({
            "id": pkey,
            "label": plabel,
            "all_ruin_pct": all_p.get("ruin_prob_pct"),
            "early_crash_ruin_pct": early_p.get("ruin_prob_pct"),
            "early_crash_median_twd_label": _fmt_twd_wan(early_p.get("median_terminal_twd")),
            "japan_1989_note": jp_note,
            "comment": comment,
        })

    # Behavior error summary rows from gate_5_behavior
    beh_order = [
        ("R2", "R2 保守（60/40 股債）"),
        ("R1", "R1 一般（50/50 台美）"),
        ("M2a_0050", "對照：100% 0050"),
        ("R3_M3a", "R3（M3a，1.3x + 濾網）"),
        ("R3_M3b", "R3（M3b，1.4x 年平衡）"),
        ("R4_M4", "對照：100% 正二（2.0x）"),
    ]
    behavior_rows = []
    for bkey, blabel in beh_order:
        b = rc_g5.get(bkey, {})
        if not b:
            continue
        behavior_rows.append({
            "label": blabel,
            "control_mean_xirr": b.get("control_mean_xirr_pct"),
            "a_loss": b.get("A_pause_6m", {}).get("mean_xirr_loss_pp"),
            "a_trigger": b.get("A_pause_6m", {}).get("trigger_rate_pct"),
            "b_loss": b.get("B_panic_sell_20pct", {}).get("mean_xirr_loss_pp"),
            "b_trigger": b.get("B_panic_sell_20pct", {}).get("trigger_rate_pct"),
            "c_loss": b.get("C_unemployed_12m_withdraw", {}).get("mean_xirr_loss_pp"),
            "c_worst": b.get("C_unemployed_12m_withdraw", {}).get("worst_xirr_loss_pp"),
            "avg_loss": b.get("average_loss_across_ABC_pp"),
        })

    # Gate 6 multiple comparison summary
    g6_mc = rc_g6.get("multiple_comparison_90ci", {})
    g6_agg_c2 = (g6_mc.get("aggressive_60_top5", {}).get("comparisons_2_to_5") or [{}])[0]
    g6_stat_m3b_prob = rc_g6.get("schemes", {}).get("stationary_avg3y", {}).get("r3_m3b_prob_mdd_lt_60pct", 32.66)

    # Load final_check results (8 loophole checks)
    fc_path = repo_root / "research" / "final_check" / "results.json"
    fc_raw = json.loads(fc_path.read_text(encoding="utf-8"))
    fc_t1 = fc_raw["test_1_aggressive_filter"]
    fc_t2 = fc_raw["test_2_estate_tax_domicile"]
    fc_t3 = fc_raw["test_3_real_bonds"]
    fc_t4 = fc_raw["test_4_lifetime_continuous"]
    fc_t5 = fc_raw["test_5_withdrawal_protection"]
    fc_t6 = fc_raw["test_6_shiller_inflation"]
    fc_t7 = fc_raw["test_7_fx_swings"]
    fc_t8 = fc_raw["test_8_different_people"]

    t1_fc = fc_t1["full_port_0band"]
    t1_fb = fc_t1["bootstrap_full_port"]
    t1_a50 = fc_t1["age50_exit_comparison"]
    t2_ov = fc_t2["overlap_validation_2010_2026"]
    t2_dca = fc_t2["full_history_dca_comparison"]
    t2_es10 = fc_t2["estate_tax_by_death_age"]["dca_10k_per_month"]
    t2_es30 = fc_t2["estate_tax_by_death_age"]["dca_30k_per_month"]
    t3_22 = fc_t3["selloff_2022_and_history"]
    t3_r30 = fc_t3["retirement_30y_comparison"]
    sel_bond = fc_t3["selected_bond_etf"]
    t4_mod_p = fc_t4["modern_tw_us_bootstrap"]["protect_20_35_plus_2"]
    t4_mod_f = fc_t4["modern_tw_us_bootstrap"]["fixed_4_plus_2"]
    t4_sh_p = fc_t4["shiller_1871_rolling"]["protect_20_35_cpi"]
    t4_sh_f = fc_t4["shiller_1871_rolling"]["fixed_4_cpi"]
    t5_mod = fc_t5["modern_30y_bootstrap"]
    t5_sh = fc_t5["shiller_30y_rolling"]
    t6_roll = fc_t6["rolling_30y_summary"]
    t6_five = fc_t6["five_crisis_paths"]
    t7_sc = fc_t7["scenarios"]
    t8_cons = fc_t8["conservative_comparison"]
    t8_pers = fc_t8["personas_by_start_age"]
    t8_alts = fc_t8["late_starter_alternatives"]

    loophole_cards = [
        {
            "num": "1",
            "title": "積極型濾網規則二選一與 50 歲出清",
            "changed": True,
            "verdict_badge": "推翻舊規則（取消濾網）",
            "summary": (
                f"舊文文字寫「跌破 200 日線全部賣出（規則 B）」，但舊回測只跑「僅新扣款切換（規則 A）」。"
                f"本次補測完整積極組合（40% 0050 + 10% 00631L + 50% VOO；註：以下規則比較數字以 CSPX 計算，VOO 2010–2026 年化約低 0.11 個百分點〔13.52% 對 13.63%〕）後發現：不用濾網、每年 1 月再平衡的規則 C "
                f"全歷史 XIRR {t1_fc['rule_C']['full_history']['xirr_pct']:.2f}%、滾動 10 年中位 XIRR {t1_fc['rule_C']['rolling_10y']['median_xirr_pct']:.2f}%、"
                f"MDD {t1_fc['rule_C']['full_history']['mdd_pct']:.2f}%（Bootstrap 破 −60% 僅 {t1_fb['rule_C']['prob_mdd_lt_60pct']:.2f}%），"
                f"勝過規則 A（滾動 {t1_fc['rule_A']['rolling_10y']['median_xirr_pct']:.2f}%）與規則 B（滾動 {t1_fc['rule_B']['rolling_10y']['median_xirr_pct']:.2f}%、"
                f"年換手 {t1_fc['rule_B']['full_history']['switches_per_year']:.2f} 次、摩擦費 {t1_fc['rule_B']['full_history']['total_fees_twd']:,.0f} 元）。"
                f"另實測 50 歲 1 月出清 00631L 可將 50–60 歲最差 10% 回撤由 {t1_a50['sell_at_55_glidepath']['dd_50_60_p10_worst_pct']:.2f}% 壓至 {t1_a50['sell_at_50']['dd_50_60_p10_worst_pct']:.2f}%，故維持 50 歲出清。"
            ),
        },
        {
            "num": "2",
            "title": "美股 ETF 選擇（SPY / VOO vs CSPX）與購買管道",
            "changed": True,
            "verdict_badge": "改推 VOO（以可近性為優先）",
            "summary": (
                f"測了 CSPX 與 VOO（2010–2026 真實價格年化 CSPX.L 為 {t2_ov['cspx_real_usd_cagr_pct']:.2f}%、扣 30% 稅之 VOO 為 {t2_ov['voo_net30_usd_cagr_pct']:.2f}%、SPY 為 {t2_ov['spy_net30_usd_cagr_pct']:.2f}%；"
                f"1997–2026 台美 50/50 XIRR 分別為 {t2_dca['CSPX_15wh']['port_50_50_xirr_pct']:.2f}% vs {t2_dca['VOO_30wh']['port_50_50_xirr_pct']:.2f}%），"
                f"報酬差距只有約 0.11 個百分點；因為本文不考慮遺產，且 VOO（費用率 0.03%、配息預扣 30%）透過台灣券商複委託即可定期定額、一般人較容易購買，最後主推 VOO。"
                f"台股維持 0050（約 0.22%）與 006208（約 0.23%）打平並列。"
            ),
        },
        {
            "num": "3",
            "title": "債券改用真實商品（SHY / VGIT / IEF）",
            "changed": True,
            "verdict_badge": f"具名升級（選定 {sel_bond}）",
            "summary": (
                f"舊研究用不可買進的 ^IRX 國庫券代表債券。改用真實公債 ETF 實測 2022 股債雙殺：SHY 單獨美元回撤 {t3_22['SHY']['bond_2022_mdd_usd_pct']:.2f}%（30/30/40 組合台幣 MDD {t3_22['SHY']['port_30_30_40_2022_mdd_twd_pct']:.2f}%）、"
                f"VGIT 單獨 {t3_22['VGIT']['bond_2022_mdd_usd_pct']:.2f}%（組合 {t3_22['VGIT']['port_30_30_40_2022_mdd_twd_pct']:.2f}%，僅深 2.43pp）、IEF 單獨 {t3_22['IEF']['bond_2022_mdd_usd_pct']:.2f}%（組合 {t3_22['IEF']['port_30_30_40_2022_mdd_twd_pct']:.2f}%）。"
                f"30 年退休模擬中，VGIT 破產率 {t3_r30['VGIT']['ruin_prob_pct']:.2f}%（剛退休遇大跌 {t3_r30['VGIT']['early_crash_ruin_prob_pct']:.2f}%、90 歲中位 {t3_r30['VGIT']['median_terminal_twd']/10000:.0f} 萬）大幅優於 SHY（{t3_r30['SHY']['ruin_prob_pct']:.2f}% / {t3_r30['SHY']['early_crash_ruin_prob_pct']:.2f}% / {t3_r30['SHY']['median_terminal_twd']/10000:.0f} 萬）且費用率僅 0.03%，正式選定 VGIT。"
            ),
        },
        {
            "num": "4",
            "title": "30 歲一路跑到 90 歲的一生連續模擬",
            "changed": False,
            "verdict_badge": "通過驗證（補齊實測）",
            "summary": (
                f"同一筆資產自 30 歲月投 1 萬（總投入 360 萬）→ 55–59 歲每年實際加 8% {sel_bond} 滑降 → 60–90 歲連續提領。"
                f"近代台美 3,000 條路徑：60 歲資產 P10 {t4_mod_p['age60_p10_twd']/10000:.0f} 萬 / P50 {t4_mod_p['age60_p50_twd']/10000:.0f} 萬（{t4_mod_p['age60_p50_multiple']:.2f} 倍） / P90 {t4_mod_p['age60_p90_twd']/10000:.0f} 萬；"
                f"90 歲前破產率 0.00%（保護版）/ {t4_mod_f['ruin_prob_pct']:.2f}%（固定 4%+2%），90 歲中位剩餘 {t4_mod_p['age90_p50_twd']/10000:.0f} 萬（此數字以 CSPX 計算；VOO 2010–2026 年化約低 0.11 個百分點〔13.52% 對 13.63%〕）。"
                f"Shiller 1871–1966 共 {t4_sh_p['cohort_count']} 條真實 60 年歷史路徑亦全數通過（保護版破產率 0.00%、固定 4%+CPI {t4_sh_f['ruin_prob_pct']:.2f}%），刪去舊文「中間各年未回測」備註。"
            ),
        },
        {
            "num": "5",
            "title": "提領保護規則與最低生活費壓力測試",
            "changed": True,
            "verdict_badge": "修正為雙軌提領規則",
            "summary": (
                f"20%/3.5% 保護規則雖將 Shiller 實際 CPI 破產率由 {t5_sh['fixed_4_cpi']['ruin_prob_pct']:.2f}% 壓至 0.00%（平均觸發 {t5_sh['protect_20_35_cpi']['avg_protection_years']:.2f} 年），"
                f"卻暴露高通膨熊市年份最低一年實質生活費 P10 跌至 {t5_sh['protect_20_35_cpi']['min_spending_real_p10_pct']:.2f}%（低於 60% 警戒線）；"
                f"而完全不調通膨的固定 4% 第 30 年實質購買力縮水 {fc_t5['decision']['flat_4_real_purchasing_power_loss_30y_pct']:.2f}%（淘汰）。"
                f"依事前標準改推雙軌：穩健首選「初始 3.5% 隨實際 CPI 調整」（155 年 0.00% 破產、生活費 100% 不打折）；首年必領 4.0% 者再強制配 20%/3.5% 保護。"
            ),
        },
        {
            "num": "6",
            "title": "1871–2026 長歷史高通膨與五大災難年",
            "changed": True,
            "verdict_badge": "改依實際 CPI 調整",
            "summary": (
                f"Shiller 1,508 條 30 年路徑顯示，1966（年化 CPI {t6_five['1966']['realized_cpi_cagr_pct']:.2f}%）與 1973（{t6_five['1973']['realized_cpi_cagr_pct']:.2f}%）高通膨年代若死守「每年固定 +2%」，"
                f"實質生活費將縮水至 {t6_five['1966']['modes']['fixed_4_plus_2']['min_real_spending_pct']:.1f}%～{t6_five['1973']['modes']['fixed_4_plus_2']['min_real_spending_pct']:.1f}%；"
                f"若改按實際 CPI 調整，1966 年固定 4% 會在第 {t6_five['1966']['modes']['fixed_4_cpi']['ruin_year_offset']:.1f} 年破產，1929 年固定 4%+2% 則在第 {t6_five['1929']['modes']['fixed_4_plus_2']['ruin_year_offset']:.1f} 年破產。"
                f"唯有「初始 3.5% + 實際 CPI」在 1929 / 1966 / 1973 / 2000（26年） / 2008（18年）五大危機年全數 100% 過關且生活費不打折。"
            ),
        },
        {
            "num": "7",
            "title": "台幣 5 年急升至 25 / 急貶至 40 匯率壓力",
            "changed": False,
            "verdict_badge": "維持 30/30/40（揭露縮水幅）",
            "summary": (
                f"退休前 5 年升值至 25 元會讓 60 歲資產與首年台幣提領額縮水 {fc_t7['decision']['pre25_year1_wd_drop_vs_base_pct']:.2f}%（由 64.0 萬降至 54.9 萬/年），但破產率仍僅 {t7_sc['pre_retire_to_25']['rules']['fixed_4_plus_2']['ruin_prob_pct']:.2f}%（保護版 0.00%）。"
                f"退休後前 5 年升值至 25 元使固定 4%+2% 破產率升至 {t7_sc['post_retire_to_25']['rules']['fixed_4_plus_2']['ruin_prob_pct']:.2f}%（保護版 0.00%）；"
                f"反直覺的是，改換 20% 台幣定存（1.5%）做 50/50 幣別避險，因少賺 30 年美債複利，固定提領破產率反而升至 {t7_sc['post_retire_to_25_twd20_hedge']['rules']['fixed_4_plus_2']['ruin_prob_pct']:.2f}%，故維持 30/30/40。"
            ),
        },
        {
            "num": "8",
            "title": "不同起跑年齡（25–45 歲）與保守型 25/25/50 獨立實測",
            "changed": True,
            "verdict_badge": "補齊實測＋新增分齡表",
            "summary": (
                f"保守型 25% 0050 + 25% VOO + 50% {sel_bond} 獨立實測全歷史 XIRR {t8_cons['cons_25_25_50']['full_history_xirr_pct']:.2f}%、MDD {t8_cons['cons_25_25_50']['full_history_mdd_pct']:.2f}%、破 −30% 機率 0.00%（此數字以 CSPX 計算；VOO 2010–2026 年化約低 0.11 個百分點〔13.52% 對 13.63%〕），正式取代「未回測」。"
                f"年齡實測顯示 25 歲（60 歲中位 {t8_pers['age_25']['fixed_10k']['age60_p50_twd']/10000:.0f} 萬）與 30 歲（{t8_pers['age_30']['fixed_10k']['age60_p50_twd']/10000:.0f} 萬）完全適用（年齡實測數字以 CSPX 計算；VOO 2010–2026 年化約低 0.11 個百分點〔13.52% 對 13.63%〕）；"
                f"45 歲起跑只靠月扣 1 萬、60 歲退中位僅 {t8_pers['age_45']['fixed_10k']['age60_p50_twd']/10000:.0f} 萬（月領 {t8_pers['age_45']['fixed_10k']['retire_yr1_monthly_wd_p50_twd']:,.0f} 元），判定不適用，改提供「月扣提至 2.7–3 萬」、「延至 65 歲退」或「35/35/30 減震＋加碼」三套具體解方。"
            ),
        },
    ]

    shiller_crisis_rows = []
    for yr_k in ("1929", "1966", "1973", "2000", "2008"):
        c = t6_five[yr_k]
        m = c["modes"]
        def _fmt_mode(md: dict[str, Any]) -> str:
            if md["ruined"]:
                return f"第 {md['ruin_year_offset']:.1f} 年破產"
            return f"存活（實質剩 {md['end_real_twd']/10000:.0f} 萬／最低生活費 {md['min_real_spending_pct']:.1f}%）"
        shiller_crisis_rows.append({
            "year": yr_k,
            "label": f"{c['label']}（{c['simulated_years']}年，CPI {c['realized_cpi_cagr_pct']:.2f}%/年）",
            "fixed_4_plus_2": _fmt_mode(m["fixed_4_plus_2"]),
            "fixed_4_cpi": _fmt_mode(m["fixed_4_cpi"]),
            "protect_20_35_cpi": _fmt_mode(m["protect_20_35_cpi"]),
            "fixed_35_cpi": _fmt_mode(m["fixed_35_cpi"]),
        })

    age_persona_rows = []
    for age_i, verdict_txt in [
        (25, "完全適用（月扣 1 萬即超標）"),
        (30, "完全適用（核心基準族群）"),
        (40, "須改月扣 14,887 元，或延至 65 歲退（中位 1,133 萬）"),
        (45, "不適用月扣 1 萬 60 歲退：改月扣 2.7–3 萬、延至 65 歲退，或改 35/35/30＋加碼"),
    ]:
        pf = t8_pers[f"age_{age_i}"]["fixed_10k"]
        pg = t8_pers[f"age_{age_i}"]["grow_3pct_yr"]
        age_persona_rows.append({
            "age": f"{age_i} 歲起跑（至 60 歲）",
            "fixed_p50_label": f"{pf['age60_p50_twd']/10000:.0f} 萬（{pf['age60_p50_multiple']:.2f} 倍／月領 {pf['retire_yr1_monthly_wd_p50_twd']:,.0f} 元）",
            "grow3_p50_label": f"{pg['age60_p50_twd']/10000:.0f} 萬（{pg['age60_p50_multiple']:.2f} 倍／月領 {pg['retire_yr1_monthly_wd_p50_twd']:,.0f} 元）",
            "dca_10m_label": f"{pf['monthly_dca_needed_for_10m_median_twd']:,.0f} 元（+3% 版 {pg['monthly_dca_needed_for_10m_median_twd']:,.0f} 元）",
            "verdict": verdict_txt,
        })

    article_peak_recovery_robustness = {
        "id": "peak_recovery_robustness",
        "slug": "peak-recovery-robustness",
        "title": "普通人該怎麼投資：從歷史高點回本到八關壓力測試",
        "subtitle": "整合歷史高點回本（peak_recovery）、八關穩健性壓力測試（robustness_check）與 8 項最終邏輯補強（final_check），給非工程師普通人的定案執行手冊",
        "takeaway": (
            f"先存 6 個月生活費當緊急預備金，累積期每月定期定額 0050（或 006208）50% + VOO 50%（費用率 0.03%、配息預扣 30%，透過台灣券商複委託購買，可用美股定期定額），每年 1 月第一個交易日再平衡；"
            f"55–59 歲每年 1 月按表加 8% {sel_bond}（3–10 年美債 ETF）至 30/30/40；60 歲退休首選初始 3.5% 隨實際 CPI 提領（155 年 0.00% 破產），若首年必領 4.0% 須強制加 20%/3.5% 保護規則。"
        ),
        "estate_tax_note": "本文以投資人自己的報酬為準，不考慮身後遺產。若在意美國遺產稅（非美國居民持有美國資產超過 6 萬美元的部分最高課 40%），可改用愛爾蘭註冊的 CSPX，需開海外券商或英股複委託，回測年化與 VOO 相近。",
        "badge": "最終定案（含 8 項補強）",
        "updated_at": "2026-11",
        "github_report": "https://github.com/diabetepanda/pandatesttrading/blob/main/research/final_check/report.md",
        "github_code": "https://github.com/diabetepanda/pandatesttrading/tree/main/research/final_check",
        "final_recommendation_card": {
            "badge": "全部數字經實測（2026-11-27 定案）",
            "items": [
                {
                    "title": "1. 緊急預備金（進場前必備）",
                    "body": "先存滿 6 個月基本生活費（台幣活存或定存），存滿前不進場投資。",
                },
                {
                    "title": "2. 累積期配置與商品代號（至 55 歲）",
                    "body": (
                        "每月固定扣款：0050（約 0.22%，可並列 006208 約 0.23%）50% ＋ 美國註冊 S&P 500 ETF「VOO」（費用率 0.03%、配息預扣 30%）50%。"
                        "購買管道：台灣券商複委託（多數提供美股定期定額）。"
                    ),
                },
                {
                    "title": "3. 再平衡與手續費紀律",
                    "body": "每年 1 月第一個交易日將股債調回目標比例，其餘 11 個月不動作；一律使用定期定額免低消管道（若單筆低消 USD 15，改為每季扣款一次）。",
                },
                {
                    "title": f"4. 滑降期逐年比例（55–59 歲，債券代號 {sel_bond}）",
                    "body": (
                        f"每年 1 月調倉（0050 / VOO / {sel_bond}，{sel_bond} 費用率 0.03%）：55 歲 46/46/8 → 56 歲 42/42/16 → 57 歲 38/38/24 → 58 歲 34/34/32 → 59 歲起 30/30/40。"
                    ),
                },
                {
                    "title": "5. 提領期規則（60 歲起，維持 30/30/40 年平衡）",
                    "body": (
                        "【穩健首選】首年領總資產 3.5%，之後每年依實際 CPI 調整（1871–2026 共 1,508 條路徑 0.00% 破產、生活費 100% 不打折）。"
                        "【高現金流替代】首年領 4.0% 依實際 CPI 調整，強制加 20%/3.5% 保護規則（1 月資產比退休當時低逾 20% 改領當下 3.5% 至回本；破產率 0.00%，極端高通膨年實質生活費 P10 可降至 42.99%）。"
                    ),
                },
                {
                    "title": "6. 保守／積極與 40–45 歲晚起跑調整",
                    "body": (
                        f"保守型：25% 0050 + 25% VOO + 50% {sel_bond} 年平衡（XIRR {t8_cons['cons_25_25_50']['full_history_xirr_pct']:.2f}%、MDD {t8_cons['cons_25_25_50']['full_history_mdd_pct']:.2f}%；此數字以 CSPX 計算，VOO 2010–2026 年化約低 0.11 個百分點〔13.52% 對 13.63%〕）。"
                        f"積極型：40% 0050 + 10% 00631L + 50% VOO，不用 200 日線濾網、每年 1 月再平衡，50 歲 1 月出清 00631L（XIRR {t1_fc['rule_C']['full_history']['xirr_pct']:.2f}%、MDD {t1_fc['rule_C']['full_history']['mdd_pct']:.2f}%；此數字以 CSPX 計算，VOO 2010–2026 年化約低 0.11 個百分點〔13.52% 對 13.63%〕）。"
                        f"45 歲起跑者月扣 1 萬 60 歲退中位僅 374 萬（此數字以 CSPX 計算），須改月扣 2.7–3 萬、延至 65 歲退，或採 35/35/30 減震並加碼。"
                    ),
                },
            ],
        },
        "execution_plan": {
            "saving_phase": [
                "先存滿 6 個月生活費當緊急預備金（台幣活存或定存），存滿前不進場投資。",
                "每月固定日期扣款：0050（或同指數 006208）50% ＋ VOO（費用率 0.03%、配息預扣 30%）50%，透過台灣券商複委託購買，可用美股定期定額。",
                "每年 1 月第一個交易日調回各 50%：賣多的那邊、買少的那邊，其他時間不動。",
                "使用定期定額免低消之台灣券商複委託；若單筆最低手續費 USD 15，改成每季扣款一次。",
            ],
            "transition_table": [
                {"age": "55", "tw0050": "46%", "spy": "46%", "bond": "8%"},
                {"age": "56", "tw0050": "42%", "spy": "42%", "bond": "16%"},
                {"age": "57", "tw0050": "38%", "spy": "38%", "bond": "24%"},
                {"age": "58", "tw0050": "34%", "spy": "34%", "bond": "32%"},
                {"age": "59", "tw0050": "30%", "spy": "30%", "bond": "40%"},
                {"age": "60 歲以後", "tw0050": "30%", "spy": "30%", "bond": "40%"},
            ],
            "transition_us_col": "VOO",
            "transition_bond_col": f"{sel_bond}（3–10 年美債）",
            "withdrawal_phase": [
                "【穩健首選】第一年提領退休當時總資產的 3.5%，之後每年提領金額依當年實際 CPI（物價通膨率，常態約 2%）調整；1871–2026 共 1,508 條 30 年路徑與五大歷史危機年破產率均為 0.00%，生活費 100% 不縮水。",
                "【高現金流替代】若退休首年必須領到 4.0%，之後每年依實際 CPI 調整，並強制加上 20%/3.5% 保護規則：若某年 1 月資產比剛退休時低超過 20%，該年改領當下總資產的 3.5%，直到資產回到退休水準再恢復（30 年破產率 0.00%，但須接受高通膨極端年實質生活費 P10 暫降為 42.99%）。",
            ],
            "transition_note": (
                f"已於最終補強（測試 4）實際逐年連續模擬 30→90 歲：30 歲月投 1 萬（總投入 360 萬）經 55–59 歲逐年加 8% {sel_bond} 滑降，"
                f"60 歲中位資產 {t4_mod_p['age60_p50_twd']/10000:.0f} 萬（P10 {t4_mod_p['age60_p10_twd']/10000:.0f} 萬 / P90 {t4_mod_p['age60_p90_twd']/10000:.0f} 萬），"
                f"90 歲前破產率 0.00%（保護版）/ {t4_mod_f['ruin_prob_pct']:.2f}%（固定 4%+2%），90 歲剩餘中位數 {t4_mod_p['age90_p50_twd']/10000:.0f} 萬（此數字以 CSPX 計算；VOO 2010–2026 年化約低 0.11 個百分點〔13.52% 對 13.63%〕）。"
            ),
        },
        "risk_tier_cards": [
            {
                "tier": "保守（最多忍受 −30%）",
                "badge": "防禦型（已實測）",
                "allocation": f"0050 25% + VOO 25% + {sel_bond} 50%，每年 1 月再平衡",
                "mdd_label": f"{t8_cons['cons_25_25_50']['full_history_mdd_pct']:.2f}%（滾動 10 年最差 {t8_cons['cons_25_25_50']['rolling_10y_worst_mdd_pct']:.2f}%、破 −30% 機率 0.00%）",
                "xirr_label": f"{t8_cons['cons_25_25_50']['full_history_xirr_pct']:.2f}%（滾動 10 年中位 {t8_cons['cons_25_25_50']['rolling_10y_median_xirr_pct']:.2f}%）",
                "details": (
                    f"本次以真實商品 {sel_bond} 獨立回測 1997–2026 全歷史，最大回撤僅 {t8_cons['cons_25_25_50']['full_history_mdd_pct']:.2f}%，"
                    f"比原先壓在 −28.77% 刀鋒邊緣的 60/40 純台股方案多出 16 個百分點安全緩衝，且 233 個滾動 10 年梯次零次跌破 −30%（此數字以 CSPX 計算；VOO 2010–2026 年化約低 0.11 個百分點〔13.52% 對 13.63%〕）。"
                ),
                "gray_note": f"同條件對照：30/30/40（{sel_bond}）全歷史 XIRR {t8_cons['bal_30_30_40']['full_history_xirr_pct']:.2f}%、MDD {t8_cons['bal_30_30_40']['full_history_mdd_pct']:.2f}%（此數字以 CSPX 計算；VOO 2010–2026 年化約低 0.11 個百分點〔13.52% 對 13.63%〕）。",
            },
            {
                "tier": "一般（最多忍受 −50%）",
                "badge": "多數人首選",
                "allocation": "0050 50% + VOO 50%（每年 1 月第一個交易日再平衡）",
                "mdd_label": f"{t2_dca['VOO_30wh']['port_50_50_mdd_pct']:.2f}%（滾動 10 年最差 {t8_cons['eq_50_50']['rolling_10y_worst_mdd_pct']:.2f}%、破 −50% 機率 0.00%*）",
                "xirr_label": f"{t2_dca['VOO_30wh']['port_50_50_xirr_pct']:.2f}%（滾動 10 年中位 {t8_cons['eq_50_50']['rolling_10y_median_xirr_pct']:.2f}%*）",
                "details": (
                    f"以 VOO（費用率 0.03%、配息預扣 30%，台灣券商複委託定期定額即可購買）搭配 0050，1997–2026 全歷史 XIRR 實測為 {t2_dca['VOO_30wh']['port_50_50_xirr_pct']:.2f}%、MDD {t2_dca['VOO_30wh']['port_50_50_mdd_pct']:.2f}%，"
                    f"略勝 SPY 版（{t2_dca['SPY_30wh']['port_50_50_xirr_pct']:.2f}%），且與愛爾蘭 CSPX（{t2_dca['CSPX_15wh']['port_50_50_xirr_pct']:.2f}%）僅差 0.07 個百分點。"
                ),
                "gray_note": "*註：上列「滾動 10 年」數字以 CSPX 計算；VOO 2010–2026 年化約低 0.11 個百分點（13.52% 對 13.63%）。",
            },
            {
                "tier": "積極（最多忍受 −60%）",
                "badge": "取消濾網（規則 C）",
                "allocation": "0050 40% + 00631L 10% + VOO 50%，不用濾網、每年 1 月再平衡；50 歲 1 月出清 00631L",
                "mdd_label": f"{t1_fc['rule_C']['full_history']['mdd_pct']:.2f}%（滾動 10 年破 −60% 為 0.00%、Bootstrap {t1_fb['rule_C']['prob_mdd_lt_60pct']:.2f}%）",
                "xirr_label": f"{t1_fc['rule_C']['full_history']['xirr_pct']:.2f}%（滾動 10 年中位 {t1_fc['rule_C']['rolling_10y']['median_xirr_pct']:.2f}%、年換手 0.99 次）",
                "details": (
                    f"測試 1 證實：在台美各半架構下（台股內 80% 0050 + 20% 00631L），不用 200 日線濾網的規則 C 報酬高於規則 A（{t1_fc['rule_A']['rolling_10y']['median_xirr_pct']:.2f}%）"
                    f"與規則 B（{t1_fc['rule_B']['rolling_10y']['median_xirr_pct']:.2f}%），且省下規則 B 每年 {t1_fc['rule_B']['full_history']['switches_per_year']:.2f} 次換手與 {t1_fc['rule_B']['full_history']['total_fees_twd']:,.0f} 元交易成本。"
                    f"50 歲 1 月出清 00631L 經 2,000 條路徑實測，能將 50–60 歲 P10 最差回撤由 {t1_a50['sell_at_55_glidepath']['dd_50_60_p10_worst_pct']:.2f}% 壓進 {t1_a50['sell_at_50']['dd_50_60_p10_worst_pct']:.2f}%。"
                    f"（此數字以 CSPX 計算；VOO 2010–2026 年化約低 0.11 個百分點〔13.52% 對 13.63%〕。）"
                ),
                "gray_note": f"對照：若只買純台股 80/20（不配美股），規則 C 全歷史 MDD {fc_t1['tw_only_0band']['rule_C']['full_history']['mdd_pct']:.2f}% 會微破 −60%，故不建議單押純台股加正二。",
            },
        ],
        "loopholes_section": {
            "intro": (
                "前兩輪研究完成後，我們整理出 8 個最可能讓實戰翻車的邏輯漏洞，先在程式執行前將「何種數字會推翻舊建議」寫死並鎖進 Git（final_check/criteria.md），"
                "再逐一補跑完整實證。結果共有 6 項促成具體修改或升級、2 項通過驗證維持原案："
            ),
            "cards": loophole_cards,
            "shiller_crisis_rows": shiller_crisis_rows,
            "age_persona_rows": age_persona_rows,
        },
        "leverage_warning": f"2 倍以上槓桿（如 100% 正二或 TQQQ）在八關測試中全部確認不建議，各關跌破 −50% 的機率高達 {r4_prob_min:.1f}%～{r4_prob_max:.1f}%（報告摘要 70%～100%）。",
        "recovery_levels": [
            {
                "level": "一般修正（跌 10%～20%）",
                "median": "約 2.5～6.9 個月",
                "examples": "台股 0050 含息中位數 2.5 個月、美股 SPY 台幣 4.1 個月、S&P 500 含息 4.9 個月、S&P 500 價格 6.9 個月，多數半年內自動站回新高。",
            },
            {
                "level": "熊市（跌 20%～50%）",
                "median": "含息約 1.7～3.2 年（20～39 個月）",
                "examples": "0050 含息跌逾 20% 中位數 20.2 個月（1.7 年）、S&P 500 含息 38.5 個月（3.2 年）；2022 升息熊市 0050 跌 −33.83% 花 24.9 個月回本，SPY 台幣因美元升值僅跌 −18.85%、14.3 個月回本。",
            },
            {
                "level": "泡沫破裂（跌超過 50%）",
                "median": "含息約 4.5～15.2 年（跨國最久逾 30 年）",
                "examples": "台股 2000 泡沫（^TWII 價格 17.3 年 / 0050 含息 7.2 年）、那斯達克 2000 泡沫（^NDX 價格 15.6 年 / QQQ 含息 15.2 年）、日經 1989 泡沫（含 +1.5% 假設股息仍花 30.9 年）、上證 2007 高點（逾 19.1 年仍未回本）、1929 美國大蕭條（^GSPC 價格 25.0 年）。",
            },
        ],
        "dividend_diff_note": "含息與不含息差非常多：新聞常說台股 2000 年套牢 17.3 年（207.6 個月），指的是「不含息」的加權價格指數 ^TWII；若把每年約 3%～4% 股息再投入買 0050，其實 7.2 年（86.0 個月）就回本，足足省下 10.1 年！美股 S&P 500 在 2008 海嘯含息版也比價格版提早約 1 年回本（4.5 年 vs 5.5 年）。",
        "slow_bear_comparison": [
            {
                "scenario": "上證 2007-10 泡沫頂點（持有 10 年）",
                "lump_sum": "單筆 100 萬 → 67.6 萬（−3.85%/年，MDD −70.44%）",
                "dca_24m": "分 24 個月投入 100 萬 → 143.8 萬（+3.70%/年，MDD −40.92%）",
                "dca_monthly": "每月定期定額 R1（本金 120 萬）→ 166.6 萬（+6.39%/年，MDD −36.93%）",
            },
            {
                "scenario": "日本 1989-12 泡沫頂點（台美同時走日股，10 年）",
                "lump_sum": "單筆 100 萬 → 55.3 萬（−5.76%/年，MDD −61.47%）",
                "dca_24m": "分 24 個月投入 100 萬 → 80.9 萬（−2.09%/年，MDD −43.63%）",
                "dca_monthly": "每月定期定額 R1（本金 120 萬）→ 125.3 萬（+0.85%/年，MDD −21.04%）",
            },
            {
                "scenario": "只有台股走日本 1989 失落行情、美股正常（台美各半 R1）",
                "lump_sum": "單押台灣正二 20 年（本金 240 萬）→ 僅剩 95.7 萬（−10.54%/年，MDD −83.63%）",
                "dca_24m": "台美各半 R1 10 年（本金 120 萬）→ 242.7 萬（+13.50%/年，MDD −13.52%）",
                "dca_monthly": "台美各半 R1 20 年（本金 240 萬）→ 313.7 萬（+2.59%/年，MDD −49.54%）",
            },
        ],
        "gates_explainer": [
            {"gate": "第 1 關：資料體檢", "desc": "檢查原始股價、拆股、配息與匯率有沒有錯價，並拆解哪些結論是靠真實 ETF 成交紀錄、哪些是靠上市前公式推算。"},
            {"gate": "第 2 關：樣本外測試", "desc": "只看過去年份（1997–2005、1997–2015）挑出當時冠軍，拿去考「當初沒看過的下一個 10 年」，檢驗是不是事後諸葛。"},
            {"gate": "第 3 關：各國崩盤情境", "desc": "把台股或美股換成日本 1989、上證 2007、英國、德國等歷史路徑（共 1,252 個滾動 10 年），測試遇上他國失落年代能不能活下來。"},
            {"gate": "第 4 關：稅金、匯率與低消", "desc": "加上 30% 美股股息稅、0.3% 換匯價差、2.11% 二代健保、12% 綜所稅、台幣連升／連貶 10 年，以及 USD 15 傳統複委託低消。"},
            {"gate": "第 5 關：真人行為錯誤", "desc": "模擬一般人遇到大跌會做的三件事：跌逾 30% 停扣半年、跌逾 40% 恐慌賣出 20%、以及股災剛好失業停扣 1 年並領 18 萬生活費。"},
            {"gate": "第 6 關：統計與抽樣檢定", "desc": "改用 1 年、3 年、5 年與 Stationary Bootstrap 區塊重抽樣各跑 5,000 條路徑，並用 90% 信賴區間檢驗前五名是不是只是運氣差距。"},
            {"gate": "第 7 關：參數敏感度", "desc": "把比例微調 ±10%、扣款日改月初／月中／月底、換再平衡頻率與均線天數，看策略會不會一微調就破防。"},
            {"gate": "第 8 關：人生階段測試", "desc": "實測手上有 100 萬該一次投還是分批、以及 60 歲存到 1,000 萬每年領 4%（加 2% 通膨）連續領 30 年會不會把錢花光。"},
        ],
        "gates_honest_note": (
            "誠實交代結果：在動手跑程式前我們先把嚴格及格標準寫死（criteria.md），結果五項原推薦裡只有 R4（2 倍以上槓桿不建議）八關全數通過；"
            "其餘 R1、R2、R3、R5 依標準全部被判「被推翻」。其中一般人組合 R1（50% 0050 + 50% SPY）在風險控制上其實非常穩（破 −50% 機率幾乎為 0%），"
            "不通過的主因有三點：(1) 第 2 關樣本外排名標準很嚴，單一 10 年裡台美各半必輸給事後最強的那單一市場（後悔差距 10.44 個百分點）；"
            "(2) 第 4 關若誤用每筆最低手續費 USD 15 的傳統複委託會大幅吃掉報酬，且在部分稅率匯率情境排名落後純美股；"
            "(3) 第 8 關證實純股票不適合直接帶進 60 歲後的退休提領期。後續我們再透過 8 項最終補強把商品代號、遺產稅、真實美債與高通膨提領規則全數定案。"
        ),
        "verdict_rows": verdict_rows,
        "behavior_rows": behavior_rows,
        "behavior_three_mistakes": [
            {
                "title": "1. 大跌時嚇到停扣（跌逾 30% 停扣半年）",
                "desc": "對 R1（50/50 台美）平均讓 XIRR 少 0.28 個百分點、對 M3b（1.4x）少 0.79 個百分點、對 2 倍正二少 1.31 個百分點；在最需要買便宜股數的時候停手，反而拉長回本時間。",
            },
            {
                "title": "2. 恐慌殺低與沒預備金被迫賣股",
                "desc": "跌逾 40% 賣出 20% 持股等回前高再買回，讓 R1 平均少 0.28 個百分點、正二少 1.08 個百分點；更致命的是沒存緊急預備金、股災失業被迫停扣 1 年並領 18 萬生活費，會讓 R1 平均少 1.39 個百分點（最慘 −5.71%）、M3b 少 2.25 個百分點（最慘 −8.64%）、正二少 3.92 個百分點（最慘 −15.59%），傷害是單純停扣的 3～5 倍。",
            },
            {
                "title": "3. 只押一個國家（賭單一國運）",
                "desc": "純買台股 0050 在 5,000 次模擬中跌破 −50% 的機率高達 19.38%，只要一半換成美股就暴降到 2.68%；若單押的國家不幸走上日本 1989 失落 20 年，單押 2 倍正二投入 240 萬只剩 95.7 萬，而台美各半靠另一半美股撐腰仍能滾出 313.7 萬。",
            },
        ],
        "withdrawal_rows": withdrawal_rows,
        "top5_stat_note": (
            f"第 6 關用 90% 信賴區間做多重比較修正後發現：積極組（−60% 上限）第 1 名到第 5 名的報酬差距中位數僅 −0.37%～+0.18%，"
            f"例如第 1 名對第 2 名的 90% 信賴區間是 [{g6_agg_c2.get('ci90_low_pp', -0.87):+.2f}%, {g6_agg_c2.get('ci90_high_pp', 1.63):+.2f}%]，全部橫跨 0%；"
            "一般 1 倍台美比例組（100% 0050 到 50/50）的 90% 信賴區間為 [−2.51%, +6.01%]，同樣橫跨 0%。"
            "白話說：排名前五的組合在統計上根本分不出高下、實質上一樣好。挑投資組合應該看「規則簡不簡單、最壞情況能不能撐過去」，不要去追回測表上小數點後兩位的微弱差距。"
        ),
        "charts": {
            "underwater": {
                "title": "歷史主要股災水下期對照（不含息價格版 vs 含息總報酬）",
                "src": "./assets/research/peak_recovery_underwater_duration_bars.png",
                "caption": "跌幅超過 50% 時水下期呈指數型拉長；把股息再投入能把台股 2000 泡沫回本時間從 17.3 年砍到 7.2 年。",
            },
            "peak_curves": {
                "title": "2000 網路泡沫與 2007/2008 金融海嘯高點進場：10 年帳戶市值曲線",
                "src": "./assets/research/peak_recovery_peak_2000_2008_equity_curves.png",
                "caption": "2000 年高點單筆買進要套牢近 7 年，每月定期定額把回本時間縮到約 1.7～2 年；2 倍以上槓桿在 2008 年腰斬再腰斬（−82%～−88%）。",
            },
            "regime_boxplots": {
                "title": "未來 10 年「上漲、盤整、下跌」三種行情下的 XIRR 分佈（5,000 次模擬）",
                "src": "./assets/research/peak_recovery_regime_xirr_boxplots.png",
                "caption": "在盤整與下跌十年裡，2 倍以上槓桿中位數轉負且大幅虧損；台美各半 1 倍（M2c）與股債 60/40（M8）在三種行情都最穩。",
            },
            "intl_crash": {
                "title": "跨國崩盤壓力測試：日本 1989 與上證 2007 高點進場資產曲線",
                "src": "./assets/research/robustness_intl_crash_equity_curves.png",
                "caption": "上證指數 19 年未創新高，持續定投 10 年反而賺到 +6.39%/年；若只有台股變日本，台美各半仍靠美股撐住正報酬。",
            },
            "verdict_heatmap": {
                "title": "R1–R5 八關穩健性壓力測試判定總表熱力圖",
                "src": "./assets/research/robustness_verdict_heatmap.png",
                "caption": "依事先寫死的嚴格標準，僅 R4（2 倍以上槓桿不建議）八關全數通過；其餘四項均依弱點修正為定案執行手冊。",
            },
            "behavior_loss": {
                "title": "三種真人行為錯誤造成的平均年化報酬（XIRR）損失",
                "src": "./assets/research/robustness_behavior_xirr_loss_bars.png",
                "caption": "股災失業被迫賣股（情境 C）的傷害是單純停扣的 3～5 倍；波動越大的高槓桿組合，人性損失越慘重。",
            },
            "withdrawal_ruin": {
                "title": "退休提領 30 年資金用完（破產）機率與存活曲線",
                "src": "./assets/research/robustness_withdrawal_ruin_prob.png",
                "caption": "呈現明顯的 U 型曲線：債券太多（30/70）80 歲後被通膨啃光、股票或槓桿太高（1.4x/2x）前十年被股災砍死，60/40 股債破產率最低。",
            },
            "fc_test1_filter": {
                "title": "補強測試 1：積極型濾網規則 A / B / C 與 50 歲出清 00631L 對比",
                "src": "./assets/research/final_check_test1_aggressive_filter_rules.png",
                "caption": "在台美各半架構下，取消濾網的規則 C 報酬最高且零盯盤換手；50 歲 1 月出清 00631L 可把 50–60 歲 P10 最差回撤壓進 −47.63%。",
            },
            "fc_test2_estate": {
                "title": "補強測試 2：美國遺產稅（SPY/VOO vs 愛爾蘭 CSPX）60 / 75 / 90 歲實拿差距",
                "src": "./assets/research/final_check_test2_estate_tax_domicile.png",
                "caption": "月投 1 萬者在 60/75/90 歲過世時，僅美股買 SPY/VOO 就會被課 105 萬～637 萬美國遺產稅；月投 3 萬更高達 474 萬～2,288 萬。",
            },
            "fc_test3_bonds": {
                "title": "補強測試 3：真實美債 ETF（SHY / VGIT / IEF）2022 股債雙殺與 30 年退休表現",
                "src": "./assets/research/final_check_test3_real_bonds_2022_retire.png",
                "caption": "VGIT 雖在 2022 單獨回撤 −12.60%，但組合台幣回撤 −12.51% 僅比 SHY 深 2.43pp，卻將 30 年破產率從 3.00% 壓至 0.97% 且費用率僅 0.03%。",
            },
            "fc_test4_lifetime": {
                "title": "補強測試 4：30→90 歲一生連續模擬資產分佈（含 55–59 歲逐年加 8% VGIT）",
                "src": "./assets/research/final_check_test4_lifetime_continuous_fan.png",
                "caption": "同一筆資產連續跑完 60 年：30 歲月投 1 萬至 60 歲中位數 1,830 萬（P10 885 萬 / P90 3,976 萬），90 歲前破產率 0.00%（保護版）。",
            },
            "fc_test5_withdrawal": {
                "title": "補強測試 5：三種提領規則破產率與實質生活費對比",
                "src": "./assets/research/final_check_test5_withdrawal_protection.png",
                "caption": "固定 4% 不調通膨第 30 年實質購買力腰斬至 45.2%；20%/3.5% 保護雖 0% 破產但高通膨年 P10 實質生活費降至 43.0%，促成新增 3.5%+CPI 穩健首選。",
            },
            "fc_test6_shiller": {
                "title": "補強測試 6：Shiller 1871–2026 五大歷史災難年退休實質資產路徑",
                "src": "./assets/research/final_check_test6_shiller_crisis_paths.png",
                "caption": "1929（固定4%+2% 第 21.2 年破產）與 1966（固定4%+CPI 第 26.7 年破產）是最殘酷試金石；改為 3.5%+CPI 或 4%+20%/3.5% 保護後五條路徑全數存活。",
            },
            "fc_test7_fx": {
                "title": "補強測試 7：台幣 5 年急升至 25 / 急貶至 40 對退休破產率與台幣生活費衝擊",
                "src": "./assets/research/final_check_test7_fx_swings_impact.png",
                "caption": "退休前升至 25 元使首年台幣生活費縮水 14.2%；退休後升至 25 元固定提領破產率升至 5.13%，若改配 20% 台幣定存反而因低息升至 7.63%。",
            },
            "fc_test8_people": {
                "title": "補強測試 8：不同起始年齡（25/30/40/45 歲）60 歲資產與保守型 25/25/50 實測",
                "src": "./assets/research/final_check_test8_different_people_ages.png",
                "caption": "25/30 歲起跑完全適用，45 歲只扣 1 萬 60 歲退中位僅 374 萬（須加碼至 2.7–3 萬或延至 65 歲退）；保守型 25/25/50 實測 MDD 僅 −13.99%。",
            },
        },
        "data_limitations": [
            "0050.TW Yahoo Finance 歷史資料只從 2009-01-02 開始（缺漏 2003–2008 年真實成交紀錄），且存在 2009 年初超過 ±7% 漲跌幅限制的錯價（單日最高 +16.55%）、2014-01-02 因 2025 年一拆四分割未回溯造成的 −75.06% 假崩盤，以及 2009–2014 年配息漏算；本研究已全數清洗修補，2009-03-10 以前則以 ^TWII 日報酬加計每年 3.0% 估計股息推算（並以 2000 年即掛牌的美股 EWT 交叉核對）。",
            "00631L（2015-08-25 前）與 TQQQ（2010-02-11 前）上市前均為公式模擬資料；與真實上市後重疊期比對顯示，模擬版未計入交換合約利差與轉倉價差，年化報酬比真實 ETF 偏樂觀約 0.8%～2.4%。",
            "測試 2（SPY / VOO vs 愛爾蘭註冊 CSPX）：CSPX 在 2010-09-15 前由 SPY 含息報酬調整 15% 預扣稅與 0.07% 費率推算，2010–2026 與真實 CSPX.L 月報酬相關係數 0.9644（CSPX.L 年化 13.63% vs VOO 扣 30% 稅 13.52%）。0050（約 0.22%）與 006208（約 0.23%）為 2025–2026 規模遞減費率估算值，查詢日期 2026-11-27。",
            "測試 3（真實美債 ETF）：SHY/IEF 上市於 2002-07、VGIT 上市於 2009-11，上市前以 ^IRX/^FVX/^TNX 存續期＋凸性公式推算（重疊月相關 0.8356～0.9895）。",
            "測試 4–6（Shiller 1871–2026 長歷史）：1871–1997 無台股資料，長歷史模擬以美股 S&P 500 含息報酬作為台股代理資料，10 年期公債殖利率推算債券報酬；公開 Shiller CSV 股息與 CPI 至 2023-06，2023-07～2026-10 以 Yahoo ^SP500TR、^TNX 與 FRED CPIAUCSL 銜接；2000 退休路徑實測 26 年、2008 退休路徑實測 18 年（未滿 30 年）。",
            "台股加權指數 ^TWII 在 Yahoo Finance 僅自 1997-07-02 開始，缺漏 1990–1997 年（未涵蓋 1990 年暴跌 −80.4%）；匯率 TWD=X 僅自 2004-03-24 起，更早期間延用 32.0230。",
        ],
        "disclaimer": {
            "text": "本研究僅供量化回測與歷史實證參考，過去績效不代表未來表現，亦不構成任何個人化投資建議。",
            "github_links": [
                {
                    "title": "最終 8 項邏輯補強報告 (final_check/report.md)",
                    "url": "https://github.com/diabetepanda/pandatesttrading/blob/main/research/final_check/report.md",
                },
                {
                    "title": "最終補強事前判斷標準 (final_check/criteria.md)",
                    "url": "https://github.com/diabetepanda/pandatesttrading/blob/main/research/final_check/criteria.md",
                },
                {
                    "title": "八關穩健性壓力測試報告 (robustness_check/report.md)",
                    "url": "https://github.com/diabetepanda/pandatesttrading/blob/main/research/robustness_check/report.md",
                },
                {
                    "title": "高點回本研究完整報告 (peak_recovery/report.md)",
                    "url": "https://github.com/diabetepanda/pandatesttrading/blob/main/research/peak_recovery/report.md",
                },
            ],
        },
    }

    full_payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "title": "深度研究 (In-Depth Research)",
        "subtitle": "Signal Desk 策略量化實證、多週期盲測與組合實驗報告",
        "disclaimer": "研究結果僅供參考，過去表現不代表未來，不構成投資建議。",
        "articles": [
            article_short,
            article_combo,
            article_exam,
            article_flow,
            article_dca,
            article_leverage,
            article_dca_grid,
            article_peak_recovery_robustness,
        ],
    }
    return full_payload


def main() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    data_dir = repo_root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    payload = build_research_dict()
    text = json.dumps(payload, ensure_ascii=False, indent=2)

    data_file = data_dir / "research.json"
    data_file.write_text(text, encoding="utf-8")
    print(f"Successfully generated {data_file}")


if __name__ == "__main__":
    main()
