# Signal Desk 報單網站＋模擬驗證艙

本專案為部署於 GitHub Pages 與 GitHub Actions 的多市場交易報單與模擬驗證系統（目前進度：**階段 1 — 專案骨架、資料來源與品質檢查**）。

- 正式網址：https://diabetepanda.github.io/pandatesttrading/
- 聲明：**本站為個人模擬驗證，非投資建議。**

## 專案結構

- `SPEC.md`：完整規格書
- `config/config.yaml`：所有可調參數與標的設定
- `control/control.json`：手動控制開關（暫停報單、解除 12% 回撤暫停）
- `events.yaml`：手動維護的高影響經濟事件日曆 (UTC)
- `index.html`、`assets/`：靜態前端（直接讀取 `data` 分支 JSON）
- `src/`：Python 3.11 資料擷取、品質檢查與排程程式
- `tests/`：單元測試（每次推送由 `.github/workflows/ci.yml` 自動執行）
- `.github/workflows/`：`ci.yml`、`scan.yml`、`daily.yml`、`weekly.yml`

## GitHub 網頁設定步驟（階段 1）

1. 登入 GitHub 帳號 `diabetepanda`，建立公開（Public）倉庫 `pandatesttrading`。
2. 將本專案所有檔案上傳到 `main` 分支根目錄。
3. 開啟 GitHub Pages：進入倉庫的 **Settings → Pages**，在 **Source** 選擇 **Deploy from a branch**，Branch 選 **main**、資料夾選 **/ (root)**，按 **Save**。
4. 設定 Actions 讀寫權限：進入 **Settings → Actions → General**，滑到底部的 **Workflow permissions**，選擇 **Read and write permissions**，按 **Save**。
5. 手動觸發第一次掃描：進入 **Actions** 分頁 → 點選左側 **Market Scan (15m)** → 點右側 **Run workflow** → 維持 `probe_all: true` → 按 **Run workflow**。

## 如何操作 `control/control.json`

因為網站為純靜態網頁，沒有後端伺服器，暫停報單或解除風控暫停請直接在 GitHub 網頁編輯 `control/control.json`：

- 暫停報單：將 `"paused": false` 改為 `true`，並填寫 `"pause_reason"`。
- 解除 12% 回撤暫停：將 `"risk_review_done": false` 改為 `true`，並填寫 `"risk_review_note"`。

## 雙層價格架構（瀏覽器即時顯示 vs. 排程模擬驗證）

網站將價格來源嚴格分為兩層，互不干擾：

1. **排程驗證層（GitHub Actions，每 15 分鐘）**：
   - 使用已收盤 K 線計算報單、成交、補倉、止損止盈、風控與模擬損益，寫入 `data` 分支。
   - 網頁每 60 秒重新讀取 `data` 分支，頂部顯示「最後排程更新 (Actions)」。
2. **瀏覽器即時顯示層（`assets/config.js`，預設每 8 秒）**：
   - 網頁打開時由瀏覽器直接呼叫免 API Key 且支援 CORS 的公開 API，**僅用於顯示**最新價、24h 漲跌與未來持倉浮動損益；**絕不觸發或改寫任何報單與模擬資料**。
   - 若即時來源連線失敗，畫面顯示「即時價格來源失敗」並改回顯示排程價，絕不填補假資料。

### 各標的瀏覽器即時價格支援清單（目前全部 18 檔皆支援）

- **加密貨幣 5 檔**（`BTCUSDT`、`ETHUSDT`、`SOLUSDT`、`BNBUSDT`、`XRPUSDT`）：優先 OKX → 備援 Binance（`data-api.binance.vision`）→ 備援 Kraken。
- **金屬商品 3 檔**（`XAUUSD`、`XAGUSD`、`COPPER`）：主來源 `api.gold-api.com`，備援與 24h 漲跌幅使用 TradingView 公開報價端點（`COMEX:GC1!`、`COMEX:SI1!`、`COMEX:HG1!`）。
- **外匯 8 檔**（`EURUSD`、`GBPUSD`、`USDJPY`、`AUDUSD`、`USDCAD`、`USDCHF`、`NZDUSD`、`EURJPY`）與 **能源商品 2 檔**（`WTI`、`NATGAS`）：使用 TradingView 公開報價端點（`FX:*`、`NYMEX:CL1!`、`NYMEX:NG1!`）。
- **選配股票（預設關閉）**：目前未啟用，無瀏覽器即時價格。

## 資料來源與排程限制

- **免 API Key 公開來源**：加密貨幣優先使用 `data-api.binance.vision`（避開美國 IP 封鎖），備援 OKX、Bybit、Kraken、Coinbase；外匯與商品優先使用 `yfinance`，備援 Stooq（Stooq 僅支援日線 `1d`）。
- **Yahoo Finance 限制**：`yfinance` 的 1 分鐘 K 線僅保留近 7 天，且對雲端 IP 可能有請求頻率限制；此外，Yahoo 在「價格無變動或無成交」的分鐘會直接略過該根 1m K 線（實測 `AUDUSD`/`NZDUSD` 約缺 49.8%、`NATGAS` 約缺 21.6%）。本系統依規格**不以假資料或前值填補**，也不放寬 10% 門檻，該週期將如實標示為「資料不穩」。
- **商品 15m 延遲與重取樣**：實測 Yahoo 期貨 `15m`（`GC=F`、`CL=F` 等）常延遲 115–145 分鐘，當 `15m` 不穩或延遲時，系統會改用**已收盤的 `1m` K 線**在程式內重新取樣（resample）成已收盤的 `15m` K 線（嚴格排除未收盤區間，避免未來函數）。
- **GitHub Actions 排程限制**：cron 排程在尖峰時段可能延遲數分鐘至數十分鐘，或偶爾略過；**新建倉庫首次設定 `schedule` 後，GitHub 排程器通常需要 30～60 分鐘以上才會開始第一次自動觸發**；若倉庫長期無活動，GitHub 也可能自動停用排程。系統採用「僅使用已收盤 K 線」與「補算重播」機制應對。

## 單元測試執行方式

```bash
pip install -r requirements.txt
pytest -v
```

## 假設與已知風險（階段 1）

1. **外匯與商品目前只有單一來源（`yfinance`）**：原訂備援 Stooq（`stooq.com/q/d/l/`）目前已啟用反爬蟲 JavaScript 驗證（Proof-of-Work），無法以程式免 Key 直接抓取 CSV，因此外匯與商品目前僅依賴 `yfinance` 單一免 Key 來源，存在單點限流或不穩風險。
2. **Bybit 對美國雲端 IP 回 403**：實測顯示 `api.bybit.com` 會封鎖美國 GitHub Actions 主機 IP，已將 `bybit` 移至加密貨幣備援順序最後（優先使用 `data-api.binance.vision`、OKX、Kraken、Coinbase）。
3. **外匯與商品休市時間**：系統假設外匯與商品週末休市為週五 22:00 UTC 至週日 22:00 UTC（計算缺 K 線與日線延遲時皆會扣除週末休市時段），商品每日結算休市約在 21:00–23:00 UTC 之間（≤ 90 分鐘），不計入缺 K 線。
4. **Python 版本**：GitHub Actions 使用 Python 3.11；本機測試使用 macOS 內建 Python 3.9.6（程式皆加上 `from __future__ import annotations` 維持相容），最終以 GitHub Actions `CI Tests` 結果為準。
