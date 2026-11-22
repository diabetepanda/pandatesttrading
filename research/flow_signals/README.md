# 大戶／法人籌碼指標預測力實證研究 (`research/flow_signals`)

本目錄包含針對 **台股三大法人／期貨／融資**、**美國 CFTC COT 大型投機者持倉**、以及 **加密貨幣（OKX 大戶多空比／主動買賣量比／資金費率／未平倉量／現貨 BTC ETF 淨流入）** 對未來價格報酬預測力的完整回測與統計檢定程式。

## 檔案結構

- `fetch_data.py`：公開資料下載與快取腳本（完全免金鑰，每次請求間隔 $\ge 1.05$ 秒，失敗最多重試 3 次）。
- `run_flow_research.py`：核心統計回測與報告生成引擎（讀取 `cache/` 離線快取，執行分組比較、Spearman 相關性與滾動穩定度、事件訊號回測、70/30 樣本內外切分、Newey-West HAC 重疊報酬標準誤修正、Benjamini-Hochberg FDR 多重比較校正與極端行情排除測試）。
- `report.md`：給非工程背景交易者閱讀的繁體中文白話研究報告。
- `results.json`：機器可讀的完整統計數據與判定結果。
- `cache/`：原始下載資料快取目錄（已加入 `.git/info/exclude`，不納入版控）。

## 如何執行（可完整重跑）

### 1. 下載或更新公開資料快取
```bash
# 若 cache/ 已存在會直接略過；若要強制重新下載請加 --refresh
.venv/bin/python research/flow_signals/fetch_data.py
```

### 2. 執行回測並產出 `results.json` 與 `report.md`
```bash
.venv/bin/python research/flow_signals/run_flow_research.py
```

## 資料來源與對齊原則（嚴格防範偷看未來）

1. **台股（`^TWII` 加權指數、`2330.TW` 台積電）**：
   - 三大法人買賣超（證交所 `BFI82U`／FinMind 公開鏡像）、外資台指期未平倉（期交所 `TAIFEX`／FinMind）、信用交易融資餘額（證交所 `MI_MARGN`）、台積電個股外資買賣超（證交所 `T86`）。
   - **對齊方式**：台股籌碼皆於第 $t$ 日收盤後（下午 15:00～16:30）公布，回測時嚴格只預測 **$t$ 日收盤之後（$t \to t+h$）** 的報酬，絕不偷看第 $t$ 日當日漲跌。
2. **美國期貨持倉報告（CFTC COT）**：
   - 官方 CFTC Public Reporting Socrata API（免金鑰）：Traders in Financial Futures (`gpe5-46if`，取 `Leveraged Funds` 淨部位) 與 Disaggregated Futures (`72hh-3qpy`，取 `Managed Money` 淨部位)。
   - **對齊方式**：COT 報告資料截止日為每週二，但於**當週週五美東時間 15:30** 才公布；回測嚴格從**下週一開盤（或之後第一個交易日開盤）**才進場計算未來 1 日、5 日（1 週）、20 日（4 週）報酬。
3. **加密貨幣（OKX Rubik 公開端點 & Farside 現貨 BTC ETF）**：
   - OKX `/api/v5/rubik/stat/...` 與 `/api/v5/public/funding-rate-history`（K 線結束時點快照，僅預測下一根 K 線起算之未來 4 小時、1 日、3 日、5 日、20 日報酬）。
   - Farside Investors 美國現貨比特幣 ETF 每日淨流入公開表格：美股收盤後至隔日凌晨（UTC）才彙整完畢，第 $D$ 日淨流入嚴格只用來預測 **$D+1$ 日起算** 的未來報酬。
