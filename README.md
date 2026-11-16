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

## 資料來源與排程限制

- **免 API Key 公開來源**：加密貨幣優先使用 `data-api.binance.vision`（避開美國 IP 封鎖），備援 OKX、Bybit、Kraken、Coinbase；外匯與商品優先使用 `yfinance`，備援 Stooq（Stooq 僅支援日線 `1d`）。
- **Yahoo Finance 限制**：`yfinance` 的 1 分鐘 K 線僅保留近 7 天，且對雲端 IP 可能有請求頻率限制。
- **GitHub Actions 排程限制**：cron 排程在尖峰時段可能延遲數分鐘至數十分鐘，或偶爾略過；若倉庫長期無活動，GitHub 可能自動停用排程。系統採用「僅使用已收盤 K 線」與「補算重播」機制應對。

## 單元測試執行方式

```bash
pip install -r requirements.txt
pytest -v
```

## 假設與已知風險（階段 1）

1. **Stooq 僅支援日線**：外匯與商品的盤中週期（`1m`、`15m`）目前僅依賴 `yfinance`，若 `yfinance` 在 GitHub Actions 上對特定期貨代碼（如 `GC=F`、`CL=F`）限流或不穩，將在階段 1 實測報告中標示並依規格停用該市場的策略 A。
2. **外匯與商品休市時間**：系統假設外匯與商品週末休市為週五 22:00 UTC 至週日 22:00 UTC，商品每日結算休市約在 21:00–23:00 UTC 之間（≤ 90 分鐘），不計入缺 K 線。
3. **本機環境限制**：開發環境（macOS）尚未安裝 Xcode Command Line Tools，因此無法在本機執行 `git` 與 `pytest`；所有測試將於推送至 GitHub 後由 `ci.yml` 自動執行驗證。
