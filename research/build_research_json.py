#!/usr/bin/env python3
"""Build static JSON for the Signal Desk "In-Depth Research" (深度研究) tab.

Compiles data chronologically for:
  - Article 1: 「短線策略能拿去比賽嗎？」(Contest short-term research: 7 strategies on 1h/15m)
  - Article 2: 「截長補短，能不能把策略組合得更好？」(Combo research: L1/L2/L3/L4/F4 & improvements)
  - Article 3: 「考試回撤規則放寬，結果會不一樣嗎？」(Exam drawdown sensitivity study)
  - Article 4: 「大戶／法人資金流回測（2026-11）」(Institutional / large-trader flow signals)
  - Article 5: 「定期定額 vs 單筆買進 vs 六種策略（10 年實證）」(DCA compare: 10y multi-asset & rolling backtest)

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

    full_payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "title": "深度研究 (In-Depth Research)",
        "subtitle": "Signal Desk 策略量化實證、多週期盲測與組合實驗報告",
        "disclaimer": "研究結果僅供參考，過去表現不代表未來，不構成投資建議。",
        "articles": [article_short, article_combo, article_exam, article_flow, article_dca],
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
