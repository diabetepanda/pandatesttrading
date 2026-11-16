# Signal Desk 報單網站＋模擬驗證艙

本專案為部署於 GitHub Pages 與 GitHub Actions 的多市場交易報單與模擬驗證系統（目前進度：**階段 7 — 最終驗收與文件**）。

- 正式網址：https://diabetepanda.github.io/pandatesttrading/
- 規格驗收檢查表：[CHECKLIST.md](./CHECKLIST.md)（逐條對照 `SPEC.md` 實作狀態與證據）
- 聲明：**本站為個人模擬驗證，非投資建議。**

## 專案結構

- `SPEC.md`：完整規格書
- `CHECKLIST.md`：規格逐條驗收檢查表（完成／部分完成／未完成與已知風險）
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

## 如何操作 `control/control.json`（不用指令列，直接在網頁修改）

網站是純靜態網頁，沒有後端伺服器；若要**手動暫停報單**，或於**回撤達 12% 暫停後解除恢復**，直接在 GitHub 網頁編輯 `control/control.json` 即可。

### 網頁編輯步驟

1. 用瀏覽器打開你的 GitHub 倉庫：`https://github.com/diabetepanda/pandatesttrading`（確認左上角分支顯示 `main`）。
2. 在檔案列表中點進 **`control`** 資料夾，再點 **`control.json`**。
3. 點檔案內容右上角的 **鉛筆圖示（Edit this file）** 進入編輯畫面。
4. 依照下面欄位說明修改文字（**注意保留雙引號 `""`、冒號 `:` 與逗號 `,`，不要刪掉大括號 `{ }`，也不要在最後一欄多加逗號**）。
5. 改好後點右上角綠色的 **`Commit changes...`** 按鈕，維持預設選項 **`Commit directly to the main branch`**，再按一次彈出視窗的 **`Commit changes`** 存檔即可。

### 每個欄位的用途、可填值與生效時間

| 欄位名稱 | 一句話用途 | 可填的值 | 改完多久生效 |
| :--- | :--- | :--- | :--- |
| `paused` | 手動緊急暫停或恢復所有新報單。 | `true`（暫停）或 `false`（正常運作，**不要加引號**） | 下一次排程執行時生效（短線 `S1`～`S3` 最慢 15 分鐘內；長線 `L1`～`L3` 為每日 00:15 UTC）。 |
| `pause_reason` | 記錄手動暫停原因，會顯示在網站頂部警示列。 | 任意文字（例如 `"週末維護暫停"` 或 `""`） | 下一次排程寫入後顯示於網站。 |
| `risk_review_done` | 帳戶回撤達 12% 被系統強制暫停後，確認人工已檢視風險的開關。 | `true`（已檢視）或 `false`（未檢視，**不要加引號**） | 下一次排程執行並通過檢核後立即解除暫停。 |
| `risk_review_note` | 解除 12% 暫停時必須填寫的人工審查備註（不可留白）。 | 任意非空白文字（例如 `"已檢查持倉並確認恢復"`） | 與 `risk_review_done` 同次排程生效，並寫入風控事件紀錄。 |
| `updated_at` | 方便你記錄最後手動修改時間的備忘欄位。 | UTC 時間字串（例如 `"2026-11-16T08:00:00Z"`） | 僅供備忘，不影響程式邏輯。 |

### 回撤達 12% 暫停後，如何手動恢復？

當風控中心或網站頂部顯示 **「回撤達 12% 暫停」** 時，單純把 `paused` 維持 `false` 並不會恢復，必須完成以下兩欄才有效：
1. 依上方步驟點開 `control/control.json` 的鉛筆圖示。
2. 將 `"risk_review_done": false` 改成 **`"risk_review_done": true`**。
3. 將 `"risk_review_note": ""` 改成有內容的備註，例如 **`"risk_review_note": "已檢查風險與持倉，同意恢復報單"`**（**兩項同時成立才會解除**）。
4. 按 **`Commit changes...`** → **`Commit changes`** 存檔。等下一次排程跑完（或手動去 Actions 跑一次），系統就會自動解除暫停並寫入一筆 `risk_review_completed` 事件。

---

## 常見狀況怎麼辦（非工程師排解指南）

### 1. 網站畫面沒有更新（或頂部出現「排程可能延遲」）
- **先強制重新整理**：在網站按 **`Cmd + Shift + R`**（Mac）或 **`Ctrl + Shift + R`**（Windows），排除瀏覽器舊快取；網站平時每 60 秒會自動重讀一次 `data` 分支。
- **看頂部「最後排程更新」時間**：若距離現在超過 45 分鐘，代表 GitHub Actions 最近沒有成功送出新資料，請接著看下面第 2、3 點。

### 2. GitHub Actions 出現紅色叉叉 ❌
1. 打開倉庫上方的 **`Actions`** 分頁，點開出現紅色叉叉的那一筆執行紀錄。
2. 點進失敗的步驟（例如 `Run scan job` 或 `Commit and push`）查看最後幾行紅字：
   - 若是 `CI Tests` 失敗：代表最近一次改動沒有通過單元測試，需修正程式後重推。
   - 若是 Yahoo / 交易所偶發連線逾時：通常等下一次排程自動重跑就會恢復，也可手動重跑一次（見第 3 點）。

### 3. 自動排程沒有跑（怎麼手動按 `Run workflow`）
GitHub 免費版的定時排程在尖峰時段偶爾會延遲或跳過，若倉庫太久沒有任何 commit 也可能被暫時停用排程：
1. 打開倉庫網頁上方的 **`Actions`** 分頁。
2. 在左側選單點選你要跑的工作流程：
   - **Market Scan (15m)**：掃描短線策略（`S1`～`S3`）、更新持倉與即時狀態（最常用）
   - **Daily Long-Term Scan (L1-L3)**：掃描長線策略（`L1`～`L3`）訊號
   - **Weekly 6-Strategy Backtest**：重跑 6 個策略 5 年以上歷史回測與組合篩選
3. 點右側灰色的 **`Run workflow`** 下拉按鈕 → 維持預設分頁 `Branch: main` → 按綠色的 **`Run workflow`** 按鈕，等約 1 分鐘出現綠色勾勾 ✅ 後回網站重新整理即可。
4. 若頁面上出現黃色提示「Scheduled workflows are disabled」，直接按提示上的 **`Enable workflow`** 即可重新啟用定時排程。

### 4. 某個標的顯示「資料不足」或「資料不穩」
- **這是依規格保護的正常現象，不是系統壞掉**：
  - 例如 `AUDUSD`、`NZDUSD`、`NATGAS` 的 1 分鐘 K 線因 Yahoo Finance 在無跳動分鐘不回傳資料，缺口超過 10%，系統依規格標示 **`資料不穩`** 並自動停用該標的之短線掃描，絕不用假資料填補。
  - 若加密貨幣出現資金費率／持倉量 **`資料不足`**，表示當時 OKX 與 Binance 公開合約端點都連不上（例如美國 IP 限制），系統依規格自動觀望不報單。
  - 若某標的在 `補倉比較` 或 `績效統計` 顯示 **`尚未驗證`**，代表已結束報單筆數還未達到設定門檻（30 筆 / 50 筆），累積足夠樣本後會自動顯示指標。

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
- **GitHub Actions 排程限制**：GitHub 排程在整點與 15 分整點（`:00/:15/:30/:45`）常出現大量工作流程排隊壅塞，且新建倉庫首次設定 `schedule` 後通常需 30～60 分鐘以上才會開始首次自動觸發。為此 `scan.yml` 已改為錯開尖峰的 **`7,22,37,52 * * * *`**（每小時 07、22、37、52 分 UTC 執行），同時保留「僅使用已收盤 K 線」與「補算重播」機制；若倉庫長期無活動，GitHub 可能自動停用排程（可在 Actions 頁面手動重新啟用，或於階段 7 加入保活機制）。
- **如何確認排程確實自動執行**：進入倉庫 **`Actions`** 分頁 → 點左側 **`Market Scan (15m)`**，列表每筆執行紀錄下方會標示觸發來源：手動執行會顯示 `manually run by diabetepanda`，由 cron 自動觸發則會顯示 **`scheduled`**。網頁上「近期排程執行紀錄」亦會顯示 `近 24 小時排程執行次數`。

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

## 假設與已知風險（階段 2）

因 `SPEC.md` 未明確定義部分數值細節，階段 2 採用以下保守預設並集中放於 `config/config.yaml`：

1. **買賣價（Bid / Ask）推算**：當資料來源無真實買賣報價時，依標的 `assumed_spread_bps` 由中間價對稱推算：`ask = mid * (1 + spread_bps / 20000)`、`bid = mid * (1 - spread_bps / 20000)`；做多進場與做空平倉用 `ask`，做空進場與做多平倉用 `bid`。
2. **滑點預設**：市價／停損／停利觸發另加 `risk.default_slippage_bps`（預設 `1.0 bps`）之不利滑價。
3. **預估爆倉價與維持保證金模型**：採逐倉等效模型計算，`initial_margin = notional / leverage`，`maintenance_margin = initial_margin * maintenance_margin_ratio`（預設 `0.50`），因此預估爆倉距離為 `entry * (1 - maintenance_margin_ratio) / leverage`；止損距離不得超過爆倉距離的 `50%`（`stop_to_liquidation_max_ratio: 0.5`）。
4. **補倉政策 E（動態波動率＋風險預算）公式**：基準階段權重預設 `[0.40, 0.35, 0.25]`，基準名目額度保留低波動上調空間（`max_notional / 1.15`）；當 `atr_ratio >= 1.2` 乘以 `0.75`、當 `atr_ratio <= 0.8` 乘以 `1.15`，且總倉位始終受 `min(max_notional, R_plan / (stop_pct + cost_pct))` 與逐階段補倉風控鎖定上限約束。
5. **隔夜／資金費率扣除**：依標的 `overnight_cost_daily_bps` 按實際持倉時間比例（`(close_ts - entry_ts) / 86400`）於平倉時扣除。
6. **12% 回撤暫停解除流程**：觸發 12% 回撤暫停後，即使之後手動編輯 `control/control.json`，系統必須同時看到 `"risk_review_done": true` 且 `"risk_review_note"` 為非空白字串，才會解除暫停並寫入 `risk_review_completed` 事件。

## 6 個統一策略與 5 年歷史回測架構（取代舊版策略）

1. **資金池與 6 個獨立子帳戶（總資金 `$100,000`）**：
   - **短線池（`$30,000`）**：`S1` RSI(2) 回檔買進（`1h`，`$10,000`）、`S2` 波動突破（`15m`，`$10,000`）、`S3` 區間來回（`15m`，`$10,000`）。
   - **長線池（`$70,000`）**：`L1` 多標的趨勢跟隨（`1d`，`$23,333.33`）、`L2` 加密強弱輪動（`1d`，`$23,333.33`）、`L3` 200 日均線趨勢過濾（`1d`，`$23,333.34`）。
2. **歷史資料來源與短線排除原則**：
   - 加密貨幣 `15m`、`1h` 回測資料改由 Binance 公開月度資料庫（`data.binance.vision/data/spot/monthly/klines` 壓縮檔，免登入／免金鑰）抓取 5 年以上完整 K 線並快取於 `.cache/binance_monthly/`；上市未滿 5 年的標的採用全部可得資料並如實標記（`TONUSDT` 依例外由備援來源處理）。
   - 外匯與商品因找不到可靠的免費 5 年 `15m`/`1h` 資料，於短線回測組合中直接排除並於網頁成績總表註明「無足夠免費歷史資料」，不用 Yahoo 1 個月資料湊數；長線日線仍使用 5～10 年資料驗證。
3. **投資組合風控與回測＝實盤一致性**：
   - 6 個策略的進出場判斷（`decide_strategy_step`／`decide_l2_rotation_step`）與子帳戶部位及同時持倉風險控制（`allocate_subaccount_position`：單筆風險 `0.75%`、同時持倉總風險上限 `5%`、同時持倉檔數上限 `5` 檔）由同一份程式執行。
   - **Crypto 衍生品過濾**：掃描加密貨幣時優先使用 OKX 公開合約端點抓取資金費率與 1h 未平倉量變化（備援 `fapi.binance.com`），取不到時判為「資料不足」／「抓取失敗」寫入 `watch_today.json`。
   - **不穩標的不填補原則**：`AUDUSD`、`NZDUSD`、`NATGAS` 的 `1m` 在 Yahoo 無跳動分鐘缺 K 線超過 10%，掃描會自動將其標為 `資料不穩` 並記錄於觀望紀錄，不產生報單。
   - **每張新報單獨立 commit**：當排程產生新報單時，會依 `SPEC.md` 1.7 先為每張新報單單獨建立一筆含報單編號的 git commit 寫入 `data` 分支，再 commit 其餘狀態檔。
4. **嚴格樣本內／樣本外切分與通過門檻（不放寬）**：
   - 依時間順序前 `70%` 為樣本內（IS，只用來挑選候選參數）、後 `30%` 為樣本外（OOS，只評估一次）；全額扣除手續費（`fee_rate` 雙邊）、點差（`assumed_spread_bps`）、滑價（`risk.default_slippage_bps`）與每日隔夜費（`overnight_cost_daily_bps`）。
   - 嚴格以「整個策略組合」評估通過門檻：具備 `>= 5` 年歷史資料、OOS 交易數 `>= 30`、期望值 `> 0`、最大回撤 `< 25%`、獲利因子（Profit Factor）`> 1.3`。
   - **歷史回測不代表未來表現**，僅作為策略驗證與篩選依據。

## 假設與已知風險（階段 6）

1. **排程並發控制與 `data` 分支合併重試**：
   - `scan.yml`、`daily.yml`、`weekly.yml` 已改用各自獨立的 concurrency group（`data-writer-scan`、`data-writer-daily`、`data-writer-weekly`，皆設 `cancel-in-progress: false`），避免 15 分鐘掃描把正在等待的每日／每週任務擠掉取消。
   - 若同時寫入 `data` 分支發生 non-fast-forward 失敗，`src/jobs/data_sync.py` 會自動執行 `git pull --rebase origin data` 並合併重疊檔案（例如 `run_log.json`、`state.json`、`signals_index.json`、`equity_curve.json` 與所有 `*.jsonl`），最多重試 `3` 次，不覆蓋其他排程資料。
2. **五個影子帳本與樣本不足隱藏原則（`config/config.yaml`）**：
   - `analytics.min_verified_trades_shadow` 預設 `30` 筆、`analytics.min_verified_trades_stats` 預設 `50` 筆、`analytics.min_cagr_days` 預設 `30` 天。
   - 當已結束交易數未達門檻時，後端 `shadow_comparison.json` 與 `performance_stats.json` 會將勝率、期望值、總報酬、CAGR、夏普與獲利因子等易誤導數值輸出為 `null`，前端網頁（`#/scaling`、`#/stats`）一律標示 **`尚未驗證`**，且絕不判定任何方法為最佳。
3. **指標公式保守預設**：
   - 年化夏普率依觀察交易頻率年化計算（無風險利率 `analytics.risk_free_rate_annual` 預設 `0.0`）；期間未滿 `30` 天時 CAGR 標示為 `僅供參考`。
   - 風險調整報酬率採用總報酬率除以 `max(1.0, max_drawdown_pct)`，資金使用效率採平均投入總名目／初始資金比例估算。
   - 若 `data` 分支尚無淨值快照或影子比較檔，所有新頁面（`#/stats`、`#/account`、`#/scaling`、`#/risk`）一律顯示空狀態，絕不使用假資料或隨機數據。
