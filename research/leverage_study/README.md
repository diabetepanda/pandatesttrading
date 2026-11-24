# 美股指數與台股指數：槓桿倍率、借貸／融資利率與跌深加碼的甜蜜點實證研究 (`research/leverage_study`)

本目錄針對美股（S&P 500 `^GSPC` / `SPY`、Nasdaq 100 `^NDX` / `QQQ`）、台股（加權指數 `^TWII`、元大台灣50 `0050.TW` 含息還原）及五檔真實槓桿 ETF（`SSO`、`UPRO`、`QLD`、`TQQQ`、`00631L.TW`），完整執行五大槓桿研究模組，並同時輸出「**全歷史最長期間（涵蓋 1997/2000/2008/2020/2022 多次大熊市）**」與「**近 10 年（2016–2026）**」雙期間對照及歷史所有月初進場之 **5 年、10 年、15 年滾動視窗檢定**。

## 檔案結構

- `run_leverage_study.py`：完整可重現的回測主程式（下載並快取 Yahoo Finance 資料、修正台灣 ETF 分割與配息還原斷層、執行五大模組、執行數值一致性驗證、自動輸出 `results.json` 與 `report.md`）。
- `results.json`：完整結構化回測數據結果。
- `report.md`：繁體中文白話完整研究報告（含一句話總結論、總表 1 槓桿倍率×利率甜蜜點矩陣、總表 2 四種槓桿工具實戰評比、總表 3 跌深加碼與 200 日線動態槓桿評比、明確實務界線與研究限制說明）。
- `cache/`：本地原始行情快取目錄（已列入 `.gitignore`，不進版控）。

## 如何重跑腳本

1. **使用本地快取完整重跑五大模組並重新產出 `results.json` 與 `report.md`**（無須連網，約 45 秒完成）：

```bash
.venv/bin/python research/leverage_study/run_leverage_study.py
```

2. **強制重新從 Yahoo Finance 下載最新行情並更新快取後執行**：

```bash
.venv/bin/python research/leverage_study/run_leverage_study.py --refresh
```

3. **僅下載／更新行情快取**：

```bash
.venv/bin/python research/leverage_study/run_leverage_study.py --fetch-only --refresh
```
