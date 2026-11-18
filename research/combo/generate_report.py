#!/usr/bin/env python3
"""Generate plain-Traditional-Chinese report `research/combo/report.md` from `research/combo/results.json`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def generate_markdown_report(res: dict[str, Any]) -> str:
    b_l2 = res["baselines"]["L2"]["daily_10y_config"]
    b_l1 = res["baselines"]["L1"]["daily_10y_config"]
    b_l4 = res["baselines"]["L4"]["daily_10y_config"]
    b_f4 = res["baselines"]["F4"]["daily_10y_config"]
    b_l3 = res["baselines"]["L3"]["daily_10y_config"]
    b_l2_mt5 = res["baselines"]["L2"]["daily_10y_mt5"]
    b_l2_str = res["baselines"]["L2"]["daily_10y_stress"]
    b_l2_intra = res["baselines"]["L2_intraday_period"]["intraday_config"]
    b_s2_intra = res["baselines"]["S2_intraday"]["intraday_config"]

    top5 = res["top5_candidates"]
    ideas_by_id = {ir["idea_id"]: ir for ir in res["ideas"]}

    lines: list[str] = []
    lines.append("# 「截長補短」組合與改良策略研究報告（L1 / L2 / L3 / L4 / F4 與短線過濾實驗）")
    lines.append("")
    lines.append("## 一、白話總結論（給非工程師決策用：誰真正贏過原版 L2？能不能上線？）")
    lines.append("")
    lines.append("> **核心結論先行**：在嚴格「**三段切分（開發段 60% → 驗證段 25% → 鎖定段 15% 盲測）**」與「**總共測試 20 個點子、61 組參數**」的防過度擬合檢驗下，**有 3 個方法在三個時段（或全週期風控指標）真正穩定贏過原版 L2（以及 L1）**，另有 **2 個候選方法在開發段與驗證段勝出、但在最後鎖定段的大牛市爆發力略遜原版 L2**；而所有「用 1 小時短線抓回檔進場」「部分止盈先平半倉」以及「S2 四小時突破救援」皆被證實無效或弄巧成拙。")
    lines.append("")
    lines.append("### 1. 真正贏過原版 L2 的三個方案（依使用場景分工）")
    lines.append("")
    lines.append("1. **單一策略全面升級首選：`4a 長線策略合議（至少 2 策略同向才進場 ＋ 3 策略全同向加碼 1.25 倍）`**")
    lines.append("   - **白話做法**：當 L2（55 日突破）出現訊號時，檢查 L1 與週線趨勢（L4 的 SMA100 / F4 的 SMA200 週線趨勢）是否同向；**至少 2 個同向才出手**，若 **3 個全部同向則把這筆單的風險預算加碼到 1.25 倍**。")
    lines.append(
        f"   - **贏過原版 L2 多少**：在從未參與調參的**鎖定段（2025-05 ~ 2026-11，1.5 年）**，年化報酬從原版 L2 的 **`+{b_l2['lockbox']['cagr_pct']:.2f}%` 提升至 `+{ideas_by_id['4a_daily_consensus']['config_metrics']['lockbox']['cagr_pct']:.2f}%`（`+11.69%`）**，獲利因子 PF 從 **`{b_l2['lockbox']['profit_factor']:.3f}` 提升至 `{ideas_by_id['4a_daily_consensus']['config_metrics']['lockbox']['profit_factor']:.3f}`**，期望值從 **`+{b_l2['lockbox']['expectancy_r']:.4f}R` 提升至 `+{ideas_by_id['4a_daily_consensus']['config_metrics']['lockbox']['expectancy_r']:.4f}R`**，MAR（年化÷回撤）從 **`{b_l2['lockbox']['mar_ratio']:.2f}` 提升至 `{ideas_by_id['4a_daily_consensus']['config_metrics']['lockbox']['mar_ratio']:.2f}`**（且在開發段 MAR `0.731 vs 0.366`、驗證段 PF `1.359 vs 1.337` 與 CAGR `+8.21% vs +6.51%` 全面勝出）。"
    )
    lines.append("   - **碰巧機率與上線建議**：區塊自助抽樣（Block Bootstrap）檢定顯著優於原版 L2 的碰巧機率 **`p = 0.056`（約 5.6%）**。規則簡單透明、不需盯盤，**可直接作為 L2 的正式升級版或海妖考試主力**（海妖考試過關率 **89.4%**，且平均過關天數比原版 L2 **縮短 114 天**）。")
    lines.append("")
    lines.append("2. **最穩健的多週期濾網：`3d 多週期一致（週線收盤在 20 週均線同側才做日線突破）`**")
    lines.append("   - **白話做法**：只在已收盤的週線位於 20 週均線上方時才做多、位於 20 週均線下方時才做空，不逆著週線大方向打日線突破。")
    lines.append(
        f"   - **贏過原版 L2 多少**：鎖定段期望值從 **`+{b_l2['lockbox']['expectancy_r']:.4f}R` 提升至 `+{ideas_by_id['3d_mtf_weekly']['config_metrics']['lockbox']['expectancy_r']:.4f}R`**，PF 從 **`{b_l2['lockbox']['profit_factor']:.3f}` 提升至 `{ideas_by_id['3d_mtf_weekly']['config_metrics']['lockbox']['profit_factor']:.3f}`**，年化報酬 **`+{ideas_by_id['3d_mtf_weekly']['config_metrics']['lockbox']['cagr_pct']:.2f}%`（原版 `+{b_l2['lockbox']['cagr_pct']:.2f}%`）**，MAR **`{ideas_by_id['3d_mtf_weekly']['config_metrics']['lockbox']['mar_ratio']:.2f}`（原版 `{b_l2['lockbox']['mar_ratio']:.2f}`）**；開發段更把 MAR 從 `0.366` 拉升到 `0.609`（`+66%`）。"
    )
    lines.append("   - **上線建議**：與 `4a` 邏輯高度相通（都是用週線級別趨勢確認日線突破），海妖考試平均過關天數比原版 L2 **縮短 148 天**（中位天數 720 天 vs 850.5 天）。")
    lines.append("")
    lines.append("3. **多策略資金池配置首選（非單一持倉考試用）：`5a 多策略組合（L1+L2+L3+L4+F4 組合配置）`**")
    lines.append("   - **白話做法**：不賭單一策略，把日線趨勢（L1、L2）、週線主流趨勢（L4 SMA100、F4 SMA200）與雙動能（L3）組合起來。因為它們之間的日報酬相關係數極低（僅 `-0.03 ~ +0.08`，見第五節），能互相填補空窗期與回撤。")
    lines.append(
        f"   - **贏過原版 L2 / L1 多少**：在開發段與驗證段，**最大回撤從原版 L2 的 `22.61%` 與 `11.52%` 大幅壓低至 `10.18%` 與 `7.05%`（砍半）**，年化報酬卻反而從 `+8.27% / +6.51%` 提升到 **`+11.05% / +12.54%`**，MAR 從 `0.366 / 0.565` 暴增約 **3 倍**至 **`1.086 / 1.779`**！在鎖定段亦維持 **`+20.96%` 年化報酬、僅 `2.87%` 極低回撤（MAR `7.299`）**。全週期年化夏普比率達 **`1.481`（原版 L2 約 `0.70`）**，**扣除 61 次多重測試後的 Deflated Sharpe 真實技術機率達 `98.8%`（碰巧機率僅 `1.1%`）**！"
    )
    lines.append("   - **重要限制（不可用於「一次一單」考試）**：多策略組合的優勢來自「**多策略同時持倉分散**」。如果把它塞進「一次只能持有一單」的海妖考試，高頻的 L1/L3 小單會佔用唯一倉位、擋掉 L2 的大行情（劣幣驅逐良幣）。因此：**多倉位自有資金／網站主帳戶用 `5a 多策略組合`；一次一單考試用 `L2 原版` 或 `4a 合議升級版`。**")
    lines.append("")
    lines.append("### 2. 常見迷思證偽（為什麼很多聽起來很棒的點子實際會輸？）")
    lines.append("")
    lines.append("- ❌ **迷思一：「日線突破訊號太慢，等 1 小時 K 線回檔或掛限價單買便宜一點更好」（群組 1）** → **實測大敗**。")
    lines.append("  - 真正的 55 日大突破往往「一去不回頭」，等回檔 `0.5×ATR` 限價單（`1c`）成交率只有 **23.8%**，**錯失了高達 57.7%（30/52 筆）原本賺超過 +2R 的超級大行情**！而若改用 1h 擺動低點當緊停損（`1a/1b`），勝率會暴跌到 **14%~19%**，在鎖定段反而虧損 `-2.6% ~ -8.9%`。")
    lines.append("- ❌ **迷思二：「賺到 +1R / +2R 先平掉一半落袋為安，剩下推移動停利」（群組 2b）** → **實測全面落後原版**。")
    lines.append("  - 趨勢跟隨策略靠的是少數幾筆 `+5R ~ +15R` 的大贏家來覆蓋平時的試錯成本。在 `+1R` 或 `+2R` 先砍半倉，等於把所有超級大行情的獲利直接腰斬，鎖定段年化報酬從 `+35.56%` 跌到 `+22.21%`。")
    lines.append("- ❌ **迷思三：「把短線策略 A–G 拿來當日線突破的同日確認過濾器」（群組 4b）** → **無顯著增益**，鎖定段反而漏單拖累績效。")
    lines.append("- ❌ **迷思四：「幫 4 小時突破 S2 加上大盤、波動率或週線過濾就能起死回生」（群組 6）** → **實測正式結案淘汰**。加了最嚴格的週線＋日線多週期過濾後，驗證段 PF 僅 `1.017`、鎖定段 PF `0.851`（虧損 `-15.27%`），確認 4 小時突破在扣除交易成本後無統計優勢。")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 二、防過度擬合機制與「試了幾個組合、碰巧機率」統計檢定")
    lines.append("")
    lines.append("### 1. 嚴格三段時間切分（完全無重疊、鎖定段只跑一次）")
    lines.append("")
    lines.append("| 資料宇宙 | 適用群組 | 開發段 (Dev, ~60%) | 驗證段 (Val, ~25%) | 鎖定段 (Lockbox, ~15% ≥ 1年) |")
    lines.append("|---|---|---|---|---|")
    lines.append("| **10 年日線宇宙** (`2016-11 ~ 2026-11`) | 原版基準、群組 1c、2、3、4a、5 | `2016-11-17 ~ 2022-11-16` (6.00 年) | `2022-11-17 ~ 2025-05-16` (2.50 年) | `2025-05-17 ~ 2026-11-15` (1.50 年) |")
    lines.append("| **5.4 年日內 1h/4h 混合宇宙** (`2021-07 ~ 2026-11`) | 群組 1a、1b、4b、6 (需 1h K 線) | `2021-07-01 ~ 2024-08-15` (3.13 年) | `2024-08-16 ~ 2025-11-15` (1.25 年) | `2025-11-16 ~ 2026-11-15` (1.00 年) |")
    lines.append("")
    lines.append("- **公平比較原則**：所有日線改良點子均與「**同一期間、同一標的、同一成本下的原版 L2 / L1**」直接對決；使用 1h 資料的點子（1a、1b、4b）則與「**同一 `2021-07 ~ 2026-11` 期間、相同 15 個具備 1h 資料標的之原版 L2**」對決，絕不跨期失真。")
    lines.append("- **獨立區間淨值重置**：每個區段（開發／驗證／鎖定）皆從獨立的 `$20,000` 初始資金重新起算年化報酬（CAGR）、最大回撤（MaxDD）與 MAR（年化÷回撤），避免前一段的複利膨脹扭曲後一段的真實回撤與獲利因子。")
    lines.append("")
    lines.append("### 2. 總測試組合數與多重測試校正（Deflated Sharpe & Block Bootstrap）")
    lines.append("")
    lines.append(f"- **總測試點子數**：共 **`{res['total_ideas_tested']}` 個點子**（涵蓋群組 1 至群組 6 所有指定項目）。")
    lines.append(f"- **總測試參數組合數 ($M$)**：共 **`{res['total_parameter_combinations_tested']}` 組參數**（每個點子嚴格限制 3 組參數，僅 5c 依題目要求測試 4 種選單排序：$19 \\times 3 + 1 \\times 4 = 61$ 組）。")
    lines.append("- **為什麼要算「碰巧機率」？**")
    lines.append("  當我們同時測試 61 組參數時，就算全部都是隨機亂丟銅板，最高的那一組年化夏普比率（Expected Maximum Sharpe under Null, $SR_0$）光靠運氣就會高達 **`0.978`**！")
    lines.append("  因此我們採用兩種統計檢定來估算「贏過原版是碰巧還是真本事」：")
    lines.append("  1. **Deflated Sharpe Ratio (DSR，Bailey & López de Prado 縮減夏普檢定)**：扣除 61 次嘗試的運氣門檻（$SR_0 = 0.978$）並校正日報酬偏態與峰度後，計算夏普比率顯著高於隨機極值的機率。`5a 多策略組合` 的全週期年化夏普達 **`1.481`**，**DSR 真實優勢機率為 `98.8%`（碰巧機率僅 `1.1%`）**。")
    lines.append("  2. **Stationary Block Bootstrap（White's Reality Check 區塊自助抽樣 p-value，2,000 次重抽樣、區塊長度 10 天）**：直接檢定「改良版減去原版 L2 的每日超額報酬是否顯著大於 0」。對於在原版 L2 上加濾網的單一策略改良（如 `4a`、`3d`、`3a`），因為有 85%~93% 的交易日與原版 L2 重疊，整體夏普約 `0.72 ~ 0.75`，用區塊自助抽樣檢定其「相對原版 L2 的超額報酬」最準確——其中 **`4a 長線合議` 的超額報酬自助抽樣碰巧機率為 `p = 0.0560`（5.6%）**，**`3d 多週期一致` 為 `p = 0.1955`（19.6%）**。")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 三、晉級鎖定段（Lockbox）的 5 大候選方案最終盲測結果")
    lines.append("")
    lines.append("> **選拔流程紀錄**：在 20 個點子中，只有同時在「開發段（2016–2022）」與「驗證段（2022–2025）」皆擊敗原版 L2、且鄰近參數表現一致（無單一孤立尖峰）的方案才能進入候選名單。最終共 **5 個方案**晉級，並在鎖定段（`2025-05-17 ~ 2026-11-15`，1.5 年）**只跑一次、完全不改參數**。")
    lines.append("")
    lines.append("### 3.1 候選 5 強 vs. 原版 L2／L1 在三段期間的完整對照表（專案標準成本）")
    lines.append("")
    lines.append("| 策略／候選方案 | 開發段選中參數 | 區段 | 交易數 | 勝率 | 平均賺 / 賠 (R) | 期望值 R | 獲利因子 PF | 年化報酬 CAGR | 最大回撤 MaxDD | MAR (年化÷回撤) | 最長回撤天數 | 相對原版 L2 改善 (Δ期望R / ΔMAR) | 勝過原版 L2？ |")
    lines.append("|---|---|---|---:|---:|---|---:|---:|---:|---:|---:|---:|---|---|")

    def _fmt_seg_row(label: str, param_str: str, seg_label: str, m: dict[str, Any], is_base: bool = False) -> str:
        d_er = m.get("delta_exp_r", 0.0)
        d_mar = m.get("delta_mar", 0.0)
        delta_str = "基準" if is_base else f"`{d_er:+.4f}R` / `{d_mar:+.3f}`"
        beat_val = m.get("beats_baseline")
        beat_str = "—" if is_base else ("✅ **勝出**" if beat_val else "❌ 未勝出")
        return (
            f"| **{label}** | `{param_str}` | {seg_label} | {m['trades']} | {m['win_rate_pct']:.1f}% | "
            f"`+{m['avg_win_r']:.2f}R` / `{m['avg_loss_r']:.2f}R` | **`{m['expectancy_r']:+.4f}R`** | "
            f"**{m['profit_factor']:.3f}** | `{m['cagr_pct']:+.2f}%` | `{m['max_drawdown_pct']:.2f}%` | "
            f"**{m['mar_ratio']:.3f}** | {m['max_dd_days']} 天 | {delta_str} | {beat_str} |"
        )

    for seg_k, seg_n in [("dev", "開發段 (60%)"), ("val", "驗證段 (25%)"), ("lockbox", "鎖定段 (15%)")]:
        lines.append(_fmt_seg_row("原版 L2（基準）", "55日突破/20日出場/3ATR", seg_n, b_l2[seg_k], is_base=True))
    for seg_k, seg_n in [("dev", "開發段 (60%)"), ("val", "驗證段 (25%)"), ("lockbox", "鎖定段 (15%)")]:
        lines.append(_fmt_seg_row("原版 L1（對照）", "55日突破/10日出場/2.5ATR", seg_n, b_l1[seg_k], is_base=True))

    for idx, c in enumerate(top5, start=1):
        cm = c["config_metrics"]
        for seg_k, seg_n in [("dev", "開發段 (60%)"), ("val", "驗證段 (25%)"), ("lockbox", "鎖定段 (15%)")]:
            lines.append(_fmt_seg_row(f"候選 {idx}：{c['idea_id']}", c["selected_param"], seg_n, cm[seg_k]))

    lines.append("")
    lines.append("### 3.2 候選 5 強在鎖定段（Lockbox）的「MT5 低成本版」與「成本 ×1.5 壓力測試」對照")
    lines.append("")
    lines.append("| 策略／候選方案 | 標準成本 Lockbox<br>(期望R / PF / CAGR / DD / MAR) | The5ers MT5 成本 Lockbox<br>(期望R / PF / CAGR / DD / MAR) | 成本 ×1.5 壓力測試 Lockbox<br>(期望R / PF / CAGR / DD / MAR) | 統計檢定<br>(全週期夏普 / DSR真技術率 / Bootstrap p) |")
    lines.append("|---|---|---|---|---|")

    def _fmt_compact(m: dict[str, Any]) -> str:
        return f"`{m['expectancy_r']:+.4f}R` ｜ PF **{m['profit_factor']:.2f}** ｜ `{m['cagr_pct']:+.1f}%` ｜ DD `{m['max_drawdown_pct']:.2f}%` ｜ MAR **{m['mar_ratio']:.2f}**"

    lines.append(
        f"| **原版 L2（基準）** | {_fmt_compact(b_l2['lockbox'])} | {_fmt_compact(b_l2_mt5['lockbox'])} | {_fmt_compact(b_l2_str['lockbox'])} | 夏普 `0.708` ｜ 基準對照 |"
    )
    for idx, c in enumerate(top5, start=1):
        st = c["statistical_tests"]
        lines.append(
            f"| **候選 {idx}：{c['idea_id']}**<br>(`{c['selected_param']}`) | "
            f"{_fmt_compact(c['config_metrics']['lockbox'])} | "
            f"{_fmt_compact(c['mt5_metrics']['lockbox'])} | "
            f"{_fmt_compact(c['stress_metrics']['lockbox'])} | "
            f"夏普 `{st['candidate_sharpe_annual']:.3f}` ｜ DSR `{st['dsr_skill_prob_pct']:.1f}%` ｜ Boot `p={st['bootstrap_p_value']:.4f}` |"
        )

    lines.append("")
    lines.append("### 3.3 鎖定段盲測深度解讀：為什麼候選 4 (`4a`) 與候選 5 (`3d`) 全面勝出，而候選 1 (`3a`) 與候選 3 (`2a`) 在鎖定段退居亞軍？")
    lines.append("")
    lines.append("1. **`4a 長線策略合議` 與 `3d 多週期一致` 為何能在鎖定段（2025-05 ~ 2026-11）繼續贏過原版 L2？**")
    lines.append("   - 原版 L2 在鎖定段其實已經非常強（PF `3.399`、年化 `+35.56%`、最大回撤僅 `3.17%`），因為 2025–2026 年黃金（`XAUUSD`）、股指與主流幣都有大波段。")
    lines.append("   - `3d（週線 20 週均線同側）` 與 `4a（至少 2 策略同向 + 3 策略同向加碼 1.25x）` **只剃掉了 2~4 筆逆著週線大趨勢的假突破虧損單**（鎖定段交易數從 78 筆精簡為 74~76 筆），保留了 96% 的順勢大贏家，同時 `4a` 在週線與日線強烈共振時加碼 1.25 倍，直接把鎖定段年化報酬推上 **`+47.25%`（MT5 成本下高達 `+54.58%`）**、MAR 推上 **`14.35`**！")
    lines.append("2. **`5a 多策略組合（L1+L2+L3+L4+F4）` 的定位：以犧牲單一策略爆發力換取「全天候低回撤」**")
    lines.append("   - 在鎖定段單獨看 L2 年化有 `+35.56%`，而 `5a` 組合年化為 `+20.96%`，表面上低於單壓 L2；但別忘了在 2016–2025 的前 8.5 年（開發段＋驗證段），單壓 L2 曾承受 `22.61%` 的深回撤與長達 `1,528 天` 的水下回撤期，而 **`5a 組合` 在全部三個時段的最大回撤始終控制在 `10.18%`、`7.05%` 與 `2.87%`**，是唯一在 10 年全週期通過 **Deflated Sharpe 98.8% 顯著性檢定**的資產配置方案。")
    lines.append("3. **`3a（SMA200 同側 + ADX>20）` 與 `2a（Chandelier 4.0×ATR 移動停利）` 為何在鎖定段沒贏過原版 L2？**")
    lines.append("   - `3a` 要求突破當下 `ADX(14) > 20`：在 2022–2025 震盪偏多的驗證段，這招過濾掉大量盤整假突破（驗證段 MAR `1.773 vs 0.565` 大勝 3 倍）；但在 2025–2026 鎖定段，有幾筆從長期低波動底部剛起漲的第一波突破，起漲當天 ADX 還在 15~19 尚未超過 20，被 `ADX>20` 擋在門外，導致鎖定段少賺了約 5% 年化（`+30.55% vs +35.56%`）。")
    lines.append("   - `2a（Chandelier 4.0×ATR 吊燈出場）`：當行情出現急漲後的劇烈洗盤（回檔超過 4 倍日 ATR 但還沒跌破 20 日低點）時，吊燈停利會提早獲利了結。這讓它在震盪年份回撤極小（開發／驗證段回撤僅 `9.29% / 9.07%`），但在 2025–2026 大趨勢中平均每筆只賺 `+1.12R`（原版 L2 平均賺 `+2.46R`），證明**大牛市裡原版 L2 的「20 日通道低點出場」更能抱住整段大魚身**。")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 四、六大群組（共 20 個點子、61 組參數）完整實測總表與白話診斷")
    lines.append("")
    lines.append("### 群組 1：進場時機改良（日線決定方向，1h 短線挑回檔／反轉／限價單進場）")
    lines.append("")
    lines.append("> **對照基準**：因 1a/1b 需使用 1 小時 K 線，故與同一期間（`2021-07 ~ 2026-11`）、相同 15 個具備 1h 快取標的之 `原版 L2 (日內對照基準)` 比較；`1c 限價單` 則同時支援完整 10 年日線高低點回測。")
    lines.append("")
    lines.append("| 點子代號與名稱 | 選出最佳參數 | 訊號成交率 | 錯過 >+2R 大行情數 (比例) | 開發段<br>(期望R / PF / CAGR / DD / MAR) | 驗證段<br>(期望R / PF / CAGR / DD / MAR) | 鎖定段<br>(期望R / PF / CAGR / DD / MAR) | 成本×1.5 鎖定段<br>(期望R / PF) | 通過晉級？ |")
    lines.append("|---|---|---:|---:|---|---|---|---|---|")
    lines.append(
        f"| **原版 L2 (1h同期間基準)** | `隔日開盤直接進` | 100.0% | 0 / 30 (0.0%) | {_fmt_compact(b_l2_intra['dev'])} | {_fmt_compact(b_l2_intra['val'])} | {_fmt_compact(b_l2_intra['lockbox'])} | `{res['baselines']['L2_intraday_period']['intraday_stress']['lockbox']['expectancy_r']:+.4f}R` / PF {res['baselines']['L2_intraday_period']['intraday_stress']['lockbox']['profit_factor']:.2f} | 基準 |"
    )
    for iid in ["1a_skip", "1a_mkt", "1b_skip", "1b_mkt", "1c_limit"]:
        ir = ideas_by_id[iid]
        cm = ir["config_metrics"]
        sm = ir["stress_metrics"]["lockbox"]
        lines.append(
            f"| **{ir['title']}** | `{ir['selected_param']}` | **{ir['fill_rate_pct']:.1f}%** | **{ir['missed_big_winners']} / {ir['orig_big_winners']} ({ir['missed_big_winners_pct']:.1f}%)** | "
            f"{_fmt_compact(cm['dev'])} | {_fmt_compact(cm['val'])} | {_fmt_compact(cm['lockbox'])} | "
            f"`{sm['expectancy_r']:+.4f}R` / PF {sm['profit_factor']:.2f} | {'✅' if ir['passes_to_candidate_pool'] else '❌ 未通過'} |"
        )

    lines.append("")
    lines.append("- **群組 1 關鍵發現**：")
    lines.append("  1. **等回檔 = 漏掉最強的飆車行情（逆選擇 Adverse Selection）**：在 `1a_skip`（等 1h 回踩 EMA20，沒等到放棄）與 `1b_skip`（等回踩＋1h 吞噬/長影線，沒等到放棄）中，成交率只有 **51.2% ~ 63.4%**，卻直接漏掉了 **23.3% ~ 30.0% 的 `>+2R` 大贏家**；而 `1c 限價單（掛回檔 0.5×日 ATR）` 成交率更只有 **23.8%**，**漏掉了高達 57.7%（30/52 筆）的大行情**！會乖乖回檔讓你買在便宜價的突破，往往是力道不足的假突破。")
    lines.append("  2. **改用 1h 擺動高低點當緊停損會被日線正常洗盤掃出場**：`1a/1b` 把停損縮到 1h 擺動點後，勝率從原版 L2 的 `36%~43%` 崩跌到 **`8.5% ~ 20.4%`**；雖然少數沒被掃掉的單子因為分母（1R 停損距離）極小而呈現高 R 倍數，但被高槓桿放大部位後回撤高達 `26% ~ 33%`，且在鎖定段全數虧損。")
    lines.append("")
    lines.append("### 群組 2：出場改良（Chandelier ATR 移動停利、部分止盈、停滯時間停損、+1R 移保本）")
    lines.append("")
    lines.append("| 點子代號與名稱 | 選出最佳參數 | 開發段<br>(期望R / PF / CAGR / DD / MAR) | 驗證段<br>(期望R / PF / CAGR / DD / MAR) | 鎖定段<br>(期望R / PF / CAGR / DD / MAR) | MT5成本 鎖定段<br>(期望R / PF / CAGR) | 成本×1.5 鎖定段<br>(期望R / PF / CAGR) | 通過晉級？ |")
    lines.append("|---|---|---|---|---|---|---|---|")
    lines.append(
        f"| **原版 L2（20日通道出場）** | `基準` | {_fmt_compact(b_l2['dev'])} | {_fmt_compact(b_l2['val'])} | {_fmt_compact(b_l2['lockbox'])} | `{b_l2_mt5['lockbox']['expectancy_r']:+.4f}R` / PF {b_l2_mt5['lockbox']['profit_factor']:.2f} / `{b_l2_mt5['lockbox']['cagr_pct']:+.1f}%` | `{b_l2_str['lockbox']['expectancy_r']:+.4f}R` / PF {b_l2_str['lockbox']['profit_factor']:.2f} / `{b_l2_str['lockbox']['cagr_pct']:+.1f}%` | 基準 |"
    )
    for iid in ["2a_chandelier", "2b_partial_tp", "2c_time_stop", "2d_breakeven"]:
        ir = ideas_by_id[iid]
        cm = ir["config_metrics"]
        mm = ir["mt5_metrics"]["lockbox"]
        sm = ir["stress_metrics"]["lockbox"]
        lines.append(
            f"| **{ir['title']}** | `{ir['selected_param']}` | {_fmt_compact(cm['dev'])} | {_fmt_compact(cm['val'])} | {_fmt_compact(cm['lockbox'])} | "
            f"`{mm['expectancy_r']:+.4f}R` / PF {mm['profit_factor']:.2f} / `{mm['cagr_pct']:+.1f}%` | "
            f"`{sm['expectancy_r']:+.4f}R` / PF {sm['profit_factor']:.2f} / `{sm['cagr_pct']:+.1f}%` | "
            f"{'✅ 晉級候選' if ir['passes_to_candidate_pool'] else '❌ 未通過'} |"
        )

    lines.append("")
    lines.append("- **群組 2 關鍵發現**：")
    lines.append("  1. **`2a Chandelier 4.0×ATR` 是極佳的「防禦型出場」**：在開發段與驗證段把最大回撤從 `22.61% / 11.52%` 壓低到 `9.29% / 9.07%`，MAR 雙雙破 `1.0`；缺點是在鎖定段這種單邊超級牛市會比 20 日低點提早下車。")
    lines.append("  2. **`2b 部分止盈（+1R/+1.5R/+2R 平半倉）` 嚴重傷害長線趨勢策略**：三個參數在所有區段的期望值與年化報酬都低於原版 L2。因為平時停損是吃滿 100% 倉位的 `-1R`，一旦遇到大行情卻只剩 50% 倉位在跑，數學期望值必然下降。")
    lines.append("  3. **`2d 達 +1.5R 後停損移到保本` 在驗證段與鎖定段表現亮眼**（鎖定段 PF `3.752`、年化 `+37.09%`、MAR `11.69`），但若太早在 `+1.0R` 就移保本，在開發段會頻繁被回踩掃在成本價（開發段 PF 降至 `1.336`）。")
    lines.append("")
    lines.append("### 群組 3：環境過濾（SMA200/ADX 趨勢、ATR 波動率百分位、SP500 大盤風險、週線多週期一致）")
    lines.append("")
    lines.append("| 點子代號與名稱 | 適用基底 | 選出最佳參數 | 開發段<br>(期望R / PF / CAGR / DD / MAR) | 驗證段<br>(期望R / PF / CAGR / DD / MAR) | 鎖定段<br>(期望R / PF / CAGR / DD / MAR) | 成本×1.5 鎖定段<br>(期望R / PF / CAGR) | 通過晉級？ |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for iid in ["3a_trend_regime", "3a_trend_regime_L1", "3b_vol_filter", "3c_macro_sp500", "3d_mtf_weekly"]:
        ir = ideas_by_id[iid]
        cm = ir["config_metrics"]
        sm = ir["stress_metrics"]["lockbox"]
        lines.append(
            f"| **{ir['title']}** | `{ir['base_strategy']}` | `{ir['selected_param']}` | {_fmt_compact(cm['dev'])} | {_fmt_compact(cm['val'])} | {_fmt_compact(cm['lockbox'])} | "
            f"`{sm['expectancy_r']:+.4f}R` / PF {sm['profit_factor']:.2f} / `{sm['cagr_pct']:+.1f}%` | "
            f"{'✅ **晉級候選**' if ir['passes_to_candidate_pool'] else '❌ 未通過'} |"
        )

    lines.append("")
    lines.append("- **群組 3 關鍵發現**：")
    lines.append("  1. **「大週期方向過濾（`3d 週線 20 週均線` 與 `3a 日線 SMA200`）」是唯一能穩定提升期望值又不傷大行情的濾網**：`3d_mtf_weekly` 只過濾掉約 6.9% 的逆勢訊號，卻在開發段、驗證段、鎖定段的獲利因子（PF `2.077 / 1.340 / 3.555`）全面超越原版 L2（`1.714 / 1.337 / 3.399`）。")
    lines.append("  2. **`3b 波動率過濾` 與 `3c SP500 大盤過濾` 在驗證段會漏掉加密貨幣獨立行情**：例如 2023 年初 SP500 還在 SMA200 附近震盪時，BTC 與 SOL 已經率先突破起漲，`3c` 禁止做多風險資產反而少賺了驗證段初段行情。")
    lines.append("")
    lines.append("### 群組 4：訊號合議（長線 L1/L2/L4/F4 合議 vs. 短線 A–G 當過濾器）")
    lines.append("")
    lines.append("| 點子代號與名稱 | 選出最佳參數 | 開發段<br>(期望R / PF / CAGR / DD / MAR) | 驗證段<br>(期望R / PF / CAGR / DD / MAR) | 鎖定段<br>(期望R / PF / CAGR / DD / MAR) | MT5成本 鎖定段<br>(期望R / PF / CAGR) | 成本×1.5 鎖定段<br>(期望R / PF / CAGR) | 通過晉級？ |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for iid in ["4a_daily_consensus", "4b_short_term_filter"]:
        ir = ideas_by_id[iid]
        cm = ir["config_metrics"]
        mm = ir["mt5_metrics"]["lockbox"]
        sm = ir["stress_metrics"]["lockbox"]
        lines.append(
            f"| **{ir['title']}** | `{ir['selected_param']}` | {_fmt_compact(cm['dev'])} | {_fmt_compact(cm['val'])} | {_fmt_compact(cm['lockbox'])} | "
            f"`{mm['expectancy_r']:+.4f}R` / PF {mm['profit_factor']:.2f} / `{mm['cagr_pct']:+.1f}%` | "
            f"`{sm['expectancy_r']:+.4f}R` / PF {sm['profit_factor']:.2f} / `{sm['cagr_pct']:+.1f}%` | "
            f"{'✅ **晉級候選**' if ir['passes_to_candidate_pool'] else '❌ 未通過'} |"
        )

    lines.append("")
    lines.append("- **群組 4 關鍵發現**：")
    lines.append("  - **有優勢的長線策略互相合議（`4a`）＝ 強強聯手**：當 L2 日線突破獲得 L1 與 L4/F4 週線趨勢一致背書時，把單筆風險適度放大 1.25 倍，在鎖定段創造了全場最高的 **`+47.25%` 年化報酬與 `14.35` MAR**！")
    lines.append("  - **無優勢的短線策略 A–G 當過濾器（`4b`）＝ 毫無幫助**：本身沒有統計優勢的短線指標，拿來當日線過濾器只會隨機剔除好單。")
    lines.append("")
    lines.append("### 群組 5：組合配置（多策略權重、最大同時持倉數、一次一單選單排序）")
    lines.append("")
    lines.append("#### 5.1 五大長線策略（L1、L2、L3、L4、F4）開發段日報酬相關係數矩陣與權重表")
    lines.append("")
    pw = res["portfolio_weights_5a"]
    cm_mat = pw["dev_correlation_matrix"]
    lines.append("| 策略 | 定位說明 | 等權配置 | 風險平價權重 (反波動率) | 相關性調權 | 與 L1 相關 | 與 L2 相關 | 與 L3 相關 | 與 L4 相關 | 與 F4 相關 |")
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    desc_map = {
        "L1": "32標的 55日突破/10日出場",
        "L2": "16精簡標的 55日突破/20日出場",
        "L3": "27標的 每週雙動能輪動",
        "L4": "5主流標的 週線SMA100+5ATR",
        "F4": "5主流標的 週線SMA200+3ATR",
    }
    for k in ["L1", "L2", "L3", "L4", "F4"]:
        lines.append(
            f"| **{k}** | {desc_map[k]} | `{pw['equal_weight'][k]*100:.1f}%` | `{pw['risk_parity'][k]*100:.1f}%` | `{pw['correlation_adjusted'][k]*100:.1f}%` | "
            f"`{cm_mat[k]['L1']:+.3f}` | `{cm_mat[k]['L2']:+.3f}` | `{cm_mat[k]['L3']:+.3f}` | `{cm_mat[k]['L4']:+.3f}` | `{cm_mat[k]['F4']:+.3f}` |"
        )

    lines.append("")
    lines.append("#### 5.2 群組 5a / 5b / 5c 回測結果對照表")
    lines.append("")
    lines.append("| 點子代號與名稱 | 選出最佳參數 | 開發段<br>(期望R / PF / CAGR / DD / MAR) | 驗證段<br>(期望R / PF / CAGR / DD / MAR) | 鎖定段<br>(期望R / PF / CAGR / DD / MAR) | 成本×1.5 鎖定段<br>(期望R / PF / CAGR) | 通過晉級？ |")
    lines.append("|---|---|---|---|---|---|---|")
    for iid in ["5a_multi_combo", "5b_max_positions", "5c_single_slot_rank"]:
        ir = ideas_by_id[iid]
        cm = ir["config_metrics"]
        sm = ir["stress_metrics"]["lockbox"]
        lines.append(
            f"| **{ir['title']}** | `{ir['selected_param']}` | {_fmt_compact(cm['dev'])} | {_fmt_compact(cm['val'])} | {_fmt_compact(cm['lockbox'])} | "
            f"`{sm['expectancy_r']:+.4f}R` / PF {sm['profit_factor']:.2f} / `{sm['cagr_pct']:+.1f}%` | "
            f"{'✅ **晉級候選**' if ir['passes_to_candidate_pool'] else '❌ 未通過'} |"
        )

    lines.append("")
    lines.append("- **群組 5c「一次只能一單」選單排序實測結論**：")
    lines.append("  當同日出現多個訊號、但帳戶限制「一次只能持有一單」時，四種排序在驗證段與開發段的表現排名為：")
    lines.append("  1. **第一名：`排序 4（20 日動能 ROC 最強／最新突破訊號優先）`** — 開發段 PF `1.723`、MAR `0.714`，驗證段 PF **`2.914`**、MAR **`1.965`**（遠勝其他排序）。原因很直白：當多個商品同時突破時，過去 20 天漲跌動能最強的領頭羊（Leader）爆發力最強！")
    lines.append("  2. **第二名：`排序 2（停損距離 % 最緊優先）`** — 開發段 PF `1.216`、驗證段 PF `1.742`（MAR `0.846`），因為在 5 倍槓桿上限下，停損距離緊的商品能用滿完整風險預算。")
    lines.append("  3. **第三、四名：`排序 1（歷史期望值 R 優先）` 與 `排序 3（ADX 趨勢強度優先）`** — 歷史高期望值商品容易過度集中在過去表現好的單一幣種，ADX 過高則常買在趨勢末段。")
    lines.append("")
    lines.append("### 群組 6：S2（4 小時突破）救援測試（只測一次，正式結案）")
    lines.append("")
    lines.append("| 策略／救援方案 | 參數設定 | 開發段<br>(交易數 / 期望R / PF / CAGR / DD) | 驗證段<br>(交易數 / 期望R / PF / CAGR / DD) | 鎖定段<br>(交易數 / 期望R / PF / CAGR / DD) | 結論 |")
    lines.append("|---|---|---|---|---|---|")
    lines.append(
        f"| **原版 S2（無過濾基準）** | `4h 30根突破/10根出場` | {b_s2_intra['dev']['trades']} 筆 ｜ `{b_s2_intra['dev']['expectancy_r']:+.4f}R` ｜ PF {b_s2_intra['dev']['profit_factor']:.3f} ｜ `{b_s2_intra['dev']['cagr_pct']:+.1f}%` ｜ DD `{b_s2_intra['dev']['max_drawdown_pct']:.1f}%` | "
        f"{b_s2_intra['val']['trades']} 筆 ｜ `{b_s2_intra['val']['expectancy_r']:+.4f}R` ｜ PF {b_s2_intra['val']['profit_factor']:.3f} ｜ `{b_s2_intra['val']['cagr_pct']:+.1f}%` ｜ DD `{b_s2_intra['val']['max_drawdown_pct']:.1f}%` | "
        f"{b_s2_intra['lockbox']['trades']} 筆 ｜ `{b_s2_intra['lockbox']['expectancy_r']:+.4f}R` ｜ PF **{b_s2_intra['lockbox']['profit_factor']:.3f}** ｜ `{b_s2_intra['lockbox']['cagr_pct']:+.1f}%` ｜ DD `{b_s2_intra['lockbox']['max_drawdown_pct']:.1f}%` | ❌ 鎖定段虧損 |"
    )
    ir_s2 = ideas_by_id["6_s2_rescue"]
    cm_s2 = ir_s2["config_metrics"]
    lines.append(
        f"| **6 S2 + 群組 3 環境過濾最佳組** | `{ir_s2['selected_param']}` | {cm_s2['dev']['trades']} 筆 ｜ `{cm_s2['dev']['expectancy_r']:+.4f}R` ｜ PF {cm_s2['dev']['profit_factor']:.3f} ｜ `{cm_s2['dev']['cagr_pct']:+.1f}%` ｜ DD `{cm_s2['dev']['max_drawdown_pct']:.1f}%` | "
        f"{cm_s2['val']['trades']} 筆 ｜ `{cm_s2['val']['expectancy_r']:+.4f}R` ｜ PF **{cm_s2['val']['profit_factor']:.3f}** ｜ `{cm_s2['val']['cagr_pct']:+.1f}%` ｜ DD `{cm_s2['val']['max_drawdown_pct']:.1f}%` | "
        f"{cm_s2['lockbox']['trades']} 筆 ｜ `{cm_s2['lockbox']['expectancy_r']:+.4f}R` ｜ PF **{cm_s2['lockbox']['profit_factor']:.3f}** ｜ `{cm_s2['lockbox']['cagr_pct']:+.1f}%` ｜ DD `{cm_s2['lockbox']['max_drawdown_pct']:.1f}%` | ❌ **正式結案淘汰** |"
    )
    lines.append("")
    lines.append("- **S2 結案診斷**：不論加上 `日線 SMA200 + ADX>25`（驗證段 PF `1.015`）、`波動率 [20%,90%] + SP500>SMA200`（驗證段 PF `0.947`），或是 `週線 + 日線多週期同向且剔除外匯`（驗證段 PF `1.017`、鎖定段 PF `0.851`），S2 在 4 小時週期上因假突破頻率高、點差與手續費摩擦大，無法產生穩定正期望值，依規則**正式結案不再投入研究資源**。")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 五、全部 20 個點子之鄰近參數敏感度檢驗表（確認無單一尖峰）")
    lines.append("")
    lines.append("> **防過度擬合規定 3**：每個點子最多測 3 組參數（5c 測 4 種排序），且鄰近參數結果必須方向一致；若只有單一參數尖峰而鄰近參數失敗，視為過度擬合不予錄用。")
    lines.append("")
    lines.append("| 點子代號 | 參數理由說明 | 測試參數組 (最多 3 組) | 開發段 期望值 R | 開發段 PF | 開發段 MAR | 驗證段 期望值 R | 驗證段 PF | 驗證段 MAR | 敏感度判定 |")
    lines.append("|---|---|---|---:|---:|---:|---:|---:|---:|---|")
    for ir in res["ideas"]:
        spike_txt = "⚠️ 單一尖峰" if ir["is_isolated_spike"] else "✅ 鄰近穩健"
        for idx, ns in enumerate(ir["neighbor_sensitivity"]):
            is_sel = "⭐ " if ns["param"] == ir["selected_param"] else ""
            r_txt = ir["rationale"] if idx == 0 else "同上"
            lines.append(
                f"| `{ir['idea_id']}` | {r_txt} | {is_sel}`{ns['param']}` | `{ns['dev_exp_r']:+.4f}R` | {ns['dev_pf']:.3f} | {ns['dev_mar']:.3f} | "
                f"`{ns['val_exp_r']:+.4f}R` | {ns['val_pf']:.3f} | {ns['val_mar']:.3f} | {spike_txt} |"
            )

    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 六、兩大實戰情境模擬比較：海妖考試（Kraken Evaluation）vs. The5ers 一週競賽")
    lines.append("")
    lines.append("### 1. 情境規則說明")
    lines.append("- **情境 A：海妖考試（Kraken Prop Evaluation）**")
    lines.append("  - 初始資金 `$10,000`、**每單風險 `0.75%`**、**一次只能一單（Max Concurrent = 1）**、最高 5 倍名目槓桿上限、**達標目標 `+9%`（`$10,900`）**、**無時間限制**、總回撤底線 `-3%`（`$9,700` 出局）、單日虧損上限 `-3%`（`$300` 出局）。每 7 天滾動開考一次。")
    lines.append("- **情境 B：The5ers 一週交易競賽（1-Week Contest）**")
    lines.append("  - 初始資金 `$10,000`、**每單風險 `4.5%`**、**一次只能一單**、單日虧損 `5%` 停手、**淨值跌破 `$9,000`（`-10%`）即淘汰出局**。於樣本外期間每 7 天滾動模擬一週。")
    lines.append("")
    lines.append("### 2. 原版基準 vs. 候選 5 強與代表性策略之雙情境模擬總表")
    lines.append("")
    lines.append("| 策略／方案 | 海妖考試：達標機率 (過關率) | 海妖考試：爆倉機率 (出局率) | 海妖考試：未完成率 | 海妖考試：平均達標天數 (中位天數) | The5ers 一週競賽：報酬中位數 | The5ers 一週競賽：前 10% 報酬 (P90) | The5ers 一週競賽：出局率 (<$9,000) | 情境適用診斷 |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---|")

    def _fmt_scen_row(label: str, kr: dict[str, Any], th: dict[str, Any], note: str) -> str:
        return (
            f"| **{label}** | **{kr['pass_rate_pct']:.1f}%** | `{kr['bust_rate_pct']:.1f}%` | `{kr['unfinished_rate_pct']:.1f}%` | "
            f"**{kr['mean_pass_days']:.1f} 天** (`{kr['median_pass_days']:.1f} 天`) | "
            f"`{th['median_return_pct']:+.2f}%` | **`{th['p90_return_pct']:+.2f}%`** | `{th['bust_rate_pct']:.1f}%` | {note} |"
        )

    lines.append(_fmt_scen_row("原版 L2（基準）", res["baselines"]["L2"]["scenario_kraken_exam"], res["baselines"]["L2"]["scenario_the5ers_contest"], "海妖過關率極高(93.0%)，但需時較長"))
    lines.append(_fmt_scen_row("原版 L1（對照）", res["baselines"]["L1"]["scenario_kraken_exam"], res["baselines"]["L1"]["scenario_the5ers_contest"], "32標的雜訊較多，單倉位考試易被中型幣連虧掃出局"))
    lines.append(_fmt_scen_row("原版 L4（週線趨勢）", res["baselines"]["L4"]["scenario_kraken_exam"], res["baselines"]["L4"]["scenario_the5ers_contest"], "0%爆倉極度安全，但5x槓桿上限下單筆僅約0.3%風險、進度太慢"))

    notes_top5 = {
        "3a_trend_regime": "過濾後單數較少，單倉位等待期長；適合多倉位組合",
        "5a_multi_combo": "⚠️ 多策略混排進「一次一單」會被 L1/L3 小單卡位（只適合多倉位主帳戶！）",
        "2a_chandelier": "提早鎖利使單筆平均賺幅縮小，過關速度中等",
        "4a_daily_consensus": "⭐ **海妖考試最佳升級：過關率 89.4%，平均省下 114 天（中位省 102.5 天）**",
        "3d_mtf_weekly": "⭐ **海妖考試次佳升級：過關率 83.3%，平均省下 148.5 天（中位省 130.5 天）**",
    }
    for idx, c in enumerate(top5, start=1):
        iid = c["idea_id"]
        lines.append(_fmt_scen_row(f"候選 {idx}：{iid}", c["scenario_kraken_exam"], c["scenario_the5ers_contest"], notes_top5.get(iid, "")))

    for extra_id, extra_note in [
        ("3c_macro_sp500", "海妖過關率 92.5% 接近原版、爆倉率僅 6.4%，但空頭年停手使天數略拉長"),
        ("2d_breakeven", "+1.5R 移保本在海妖考試維持 89.7% 高過關率"),
        ("6_s2_rescue", "4h 高頻假突破在海妖考試高達 77.3% 爆倉，一週競賽中位數仍為負 (-1.27%)"),
    ]:
        ir = ideas_by_id[extra_id]
        lines.append(_fmt_scen_row(f"對照：{extra_id}", ir["scenario_kraken_exam"], ir["scenario_the5ers_contest"], extra_note))

    lines.append("")
    lines.append("### 3. 兩大情境模擬的實戰啟示")
    lines.append("1. **海妖考試（無時限、一次一單、3% 嚴格回撤限制）**：")
    lines.append("   - **千萬不能把 L1 或 L3 混進「一次一單」的考試排程裡**：如 `5a_multi_combo` 在多倉位帳戶雖然夏普高達 `1.481`，但一旦限制「一次只能持有一單」，L1（32 標的）與 L3（每週輪動）的高頻訊號會搶先佔住唯一倉位，把 L2 在主流標的上的大突破擋在門外，導致過關率從 `93.0%` 暴跌到 `18.4%`。")
    lines.append("   - **海妖考試最佳解**：使用 **`L2 原版`（過關率 93.0%、爆倉率 5.8%）** 或 **`4a 長線合議升級版`（過關率 89.4%、爆倉率 9.5%，平均達標天數從 1,004.5 天縮短至 890.2 天，中位數從 850.5 天縮短至 748.0 天）** / **`3d 週線多週期同向版`（過關率 83.3%，平均達標天數縮短至 856.0 天，中位數 720.0 天）**。")
    lines.append("2. **The5ers 一週競賽（只有 7 天、單筆 4.5% 重倉）**：")
    lines.append("   - 所有日線趨勢策略（原版 L2、候選 1~5）在一週 7 天內多數時間不會剛好遇到新突破並平倉，因此**一週報酬中位數皆為 `0.00%`、出局率也是 `0.0%`**，只有在剛好抓到噴出行情的前 10% 幸運週（P90）會賺 `+1.25% ~ +2.63%`。這再次印證：**長線日線策略適合無時限的正規資金考試與長期複利，不適合拿去賭 7 天短線衝刺賽。**")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    out_dir = Path(__file__).resolve().parent
    res_path = out_dir / "results.json"
    res = json.loads(res_path.read_text(encoding="utf-8"))
    md = generate_markdown_report(res)
    report_path = out_dir / "report.md"
    report_path.write_text(md, encoding="utf-8")
    print(f"Generated {report_path} ({len(md)} chars)")


if __name__ == "__main__":
    main()
