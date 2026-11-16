# Signal Desk 規格驗收檢查表（SPEC.md 對照）

本檢查表逐條核對 [SPEC.md](./SPEC.md) 的硬性限制、功能需求、風控規則、模擬引擎與部署規範，並如實記錄實作狀態、程式／測試證據與已知限制（不隱瞞、不將部分完成標記為完成）。

> 說明：`完成`＝功能、程式與測試皆已落地；`部分完成`＝核心功能已可用但仍有選配項目、實測資料源限制或輔助細項未做；`未完成`＝規格中標示選配（預設關閉）且尚未實作。

---

## 一、核心硬性限制（SPEC 第 1 節）

| 項目 | 狀態 | 證據（檔案／函式／測試） | 說明與已知風險 |
| :--- | :---: | :--- | :--- |
| 1.1 無帳戶綁定、API Key 輸入、登入、註冊、付款、用戶系統 | 完成 | [index.html](index.html)、[assets/app.js](assets/app.js) | 全站為純靜態網頁，無任何帳號、登入、付款或 API Key 輸入欄位。 |
| 1.2 無真實下單功能；每頁頂部與每張報單標示「模擬驗證中，非投資建議」 | 完成 | [index.html](index.html) 頂部列與各頁面標題、[assets/app.js](assets/app.js) `renderSignalDetail` / `formatSignalCopyText`、[test_config.py](tests/test_config.py) | 全站頂部、單張報單頁、複製報單文字與階段 6 各新頁面皆固定標示「模擬驗證中，非投資建議」。 |
| 1.3 行情資料只使用免 API Key 公開來源 | 完成 | [src/data/adapters.py](src/data/adapters.py)（`BinanceVisionAdapter`、`OkxAdapter`、`KrakenAdapter`、`CoinbaseAdapter`、`BybitAdapter`、`YFinanceAdapter`、`StooqAdapter`）、[assets/app.js](assets/app.js) | 後端與瀏覽器端皆僅使用公開免 Key 來源。 |
| 1.4 點位一律由確定性程式規則計算，不由 LLM 決定 | 完成 | [src/strategies/six_strategies.py](src/strategies/six_strategies.py)、[src/risk/manager.py](src/risk/manager.py)、[src/scaling/policies.py](src/scaling/policies.py) | 所有入場、止損、止盈與子帳戶倉位大小皆為純 Python 數學規則計算。 |
| 1.5 訊號只使用已收盤 K 線計算，避免未來函數 | 完成 | [src/data/resample.py](src/data/resample.py)、[src/indicators/technical.py](src/indicators/technical.py)、[test_resample.py](tests/test_resample.py)、[test_six_strategies.py](tests/test_six_strategies.py) | 未收盤 K 線與未完整桶於重取樣及評估前即剔除；嚴格隔離 30% 樣本外資料。 |
| 1.6 內部時間一律 UTC，介面顯示 Asia/Taipei (UTC+8) | 完成 | `src/` 各模組均採 `timezone.utc` 輸出 `Z` 格式；[assets/app.js](assets/app.js) `formatTaipeiTime`（`timeZone: "Asia/Taipei"`） | — |
| 1.7 報單發布後原始內容鎖定、只追加更新事件、保存輸入快照、獨立 git commit | 完成 | [src/signals/store.py](src/signals/store.py) `publish_signal` / `append_update`、[.github/workflows/scan.yml](.github/workflows/scan.yml)、[.github/workflows/daily.yml](.github/workflows/daily.yml)、[test_signals.py](tests/test_signals.py) | 每張新報單於 `data` 分支先以 `feat(signal): publish <ID>` 獨立 commit 再送出其餘狀態。 |
| 1.8 只有通過風控審核才發布；被拒記為觀望紀錄 | 完成 | [src/engine/simulator.py](src/engine/simulator.py) `submit_candidate_signal`、[test_risk.py](tests/test_risk.py) `test_risk_rejected_signal_not_published_and_logged_to_watch_log` | — |
| 1.9 公開倉庫無金鑰／私人資訊／個人聯絡方式；頁尾標示「本站為個人模擬驗證，非投資建議」 | 完成 | [index.html](index.html) `<footer>`、[.gitignore](.gitignore)、全 repo 關鍵字掃描無機密／私人信箱 | Git 設定使用 `@users.noreply.github.com`；頁尾固定標示免責聲明。 |

---

## 二、網頁設計與頁面清單（SPEC 第 2、4.2 節）

| 項目 | 狀態 | 證據（檔案／函式／測試） | 說明與已知風險 |
| :--- | :---: | :--- | :--- |
| 2.0 深色主題、手機響應式、60 秒重讀、頂部淨值／三燈／45 分鐘延遲警示 | 完成 | [assets/style.css](assets/style.css)、[assets/config.js](assets/config.js) `DATA_REFRESH_MS: 60000`、`STALE_MINUTES: 45`、[assets/app.js](assets/app.js) `updateLights` | 瀏覽器即時價格每 8 秒僅供顯示；斷線時顯示失敗提示而不填假資料。 |
| 2.1 報單牆（分頁、篩選、頂部摘要含 <30 筆樣本不足、今日觀望） | 完成 | [index.html](index.html) `#view-wall`、[assets/app.js](assets/app.js) `renderWallSummary` / `renderWallCards` / `renderWatchToday` | — |
| 2.2 單張報單頁（K 線圖標示、事件時間軸、對照表、快照、hash 路由 `#/signal/`） | 完成 | [index.html](index.html) `#view-signal`、[assets/app.js](assets/app.js) `renderSignalDetail` / `renderSignalChart` | 使用固定版本 CDN `lightweight-charts@4.2.1`。 |
| 2.3 成績總表（篩選、R 值、美元損益、持倉時間、出場原因） | 完成 | [index.html](index.html) `#view-results`、[assets/app.js](assets/app.js) `renderResultsTable` | — |
| 2.4 績效統計（各指標、分組、樣本不足標示「尚未驗證」） | 完成 | [index.html](index.html) `#view-stats`、[src/engine/analytics.py](src/engine/analytics.py) `build_performance_stats`、[assets/app.js](assets/app.js) `renderPerformanceStats` | 門檻集中於 `config.yaml`（預設 50 筆）；未達門檻隱藏勝率與報酬數字。 |
| 2.5 模擬帳戶（資金曲線、持倉明細、實際槓桿／保證金／即時浮動損益／預估 SL 與 TP1/TP2 損益） | 完成 | [index.html](index.html) `#view-account`、[src/engine/analytics.py](src/engine/analytics.py) `export_equity_curve`、[assets/app.js](assets/app.js) `renderSimulatedAccount` | — |
| 2.6 單一真實模擬帳本（移除舊多帳本分段，與回測共用 `allocate_subaccount_position`） | 完成 | [src/engine/simulator.py](src/engine/simulator.py) `SimulationEngine`、[src/risk/manager.py](src/risk/manager.py) `check_new_signal` | 與回測共用相同子帳戶倉位與風險預算公式。 |
| 2.7 風控中心（各上限儀表、群組曝險、爆倉距離、事件紀錄） | 完成 | [index.html](index.html) `#view-risk`、[src/engine/analytics.py](src/engine/analytics.py) `build_risk_status`、[assets/app.js](assets/app.js) `renderRiskCenter` | — |
| 2.8 市場與資料（連續性檢查、經濟事件日曆） | 完成 | [index.html](index.html) `#view-data`、[assets/app.js](assets/app.js) `renderInstruments` / `renderDataExtras` | — |
| 2.9 成績總表：6 個策略回測比較結果（樣本內外對照＋「歷史不代表未來」） | 完成 | [index.html](index.html) `#backtest-section`、[assets/app.js](assets/app.js) `renderBacktestResults` | 顯示 `S1`～`S3`、`L1`～`L3` 通過狀態、OOS 交易數、期望值、PF、最大回撤、適用標的數與排除說明。 |
| 2.10 系統狀態與設定（健康度、排程紀錄、只讀設定、錯誤日誌） | 部分完成 | [index.html](index.html) `#view-data`（健康度與排程紀錄）、[assets/app.js](assets/app.js) `renderRunLog` | 資料來源健康度與近期排程紀錄已完成且全站無任何金鑰欄位；尚未獨立建置「錯誤日誌專區」與「`config.yaml` 只讀設定檢視頁」。 |
| 2.11 一鍵複製報單文字 & `control/control.json` 控制 | 完成 | [assets/app.js](assets/app.js) `formatSignalCopyText`、[control/control.json](control/control.json)、[README.md](README.md) | — |
| 4.2 GitHub Pages `/pandatesttrading/` 相對路徑與 hash 路由 | 完成 | [index.html](index.html)、[assets/app.js](assets/app.js)、[test_config.py](tests/test_config.py) `test_frontend_assets_and_relative_paths` | — |

---

## 三、技術架構、排程與資料（SPEC 第 3.6、4、5、14、16 節）

| 項目 | 狀態 | 證據（檔案／函式／測試） | 說明與已知風險 |
| :--- | :---: | :--- | :--- |
| 4.1 純 GitHub 部署（Actions + Pages + `data` 分支 JSON/JSONL，無自架伺服器） | 完成 | `.github/workflows/`、[index.html](index.html)、[assets/config.js](assets/config.js) | — |
| 4.3 工作流程與並發寫入保護（`ci.yml`、`scan.yml`、`daily.yml`、`weekly.yml`） | 完成 | [.github/workflows/](.github/workflows/)、[src/jobs/data_sync.py](src/jobs/data_sync.py)、[test_workflow_concurrency.py](tests/test_workflow_concurrency.py) | 三個寫入排程已使用獨立 concurrency group 並具備 `git pull --rebase` 與 JSON/JSONL 合併重試（最多 3 次）。 |
| 4.4 排程延遲檢查（20m）、補算重播（`last_processed_ts`）、錯峰排程與保活 | 部分完成 | [src/engine/simulator.py](src/engine/simulator.py) `replay_bars`、[.github/workflows/scan.yml](.github/workflows/scan.yml)（`7,22,37,52 * * * *`） | 延遲保護、補算重播與排程健康度皆已完成；尚未新增獨立的定期保活 workflow（若 repo 長期無互動需於 Actions 手動重新啟用 cron）。 |
| 5.1 52 檔標的資料擷取、連續性檢查與不穩排除 | 部分完成 | [config/config.yaml](config/config.yaml)、[src/data/adapters.py](src/data/adapters.py) (`BinanceMonthlyArchiveFetcher`、`DukascopyArchiveFetcher`)、[src/data/quality.py](src/data/quality.py)、[src/data/resample.py](src/data/resample.py) | 加密貨幣透過 Binance 月度壓縮檔快取可取得 5 年以上 `15m`/`1h`；黃金於 Dukascopy 不可用時以 Binance `PAXGUSDT` 5 年資料備援並註明；無 5 年分時來源者標示「資料不足，觀察中」；4 大指數（`NAS100`、`SP500`、`JP225`、`TWII`）以 `yfinance` 取 10 年日線且不進 15m 掃描；`AUDUSD`/`NZDUSD`/`NATGAS` 1m 缺值 >10% 標示「資料不穩」。 |
| 5.2 選配股票市場（預設關閉）與事件日曆 | 部分完成 | [config/config.yaml](config/config.yaml)、[events.yaml](events.yaml)、[src/jobs/scan.py](src/jobs/scan.py) | 股票依規格預設關閉；事件日曆目前讀取手動維護之 `events.yaml`，未串接外部免 Key 公開週曆 API。 |
| 14 資料檔切分管理（按月分檔、報單獨立鎖定檔） | 部分完成 | [src/signals/store.py](src/signals/store.py)、[src/engine/simulator.py](src/engine/simulator.py) | `equity_snapshots`、`fills`、`watch_log`、`signal_updates` 已採 `YYYY-MM.jsonl` 按月分檔，`signals/` 每張獨立檔案；`signals_index.json` 與 `trades.json` 尚未額外按月歸檔。 |
| 3.6 選配通知（Telegram Bot / Webhook，預設關閉） | 未完成 | — | 規格標記為選配（預設關閉），尚未實作推送模組。 |
| 16 選配 AI 說明層（預設關閉） | 未完成 | — | 規格標記為選配（預設關閉），目前僅使用確定性程式規則產生報單理由。 |

---

## 四、帳戶、風控、策略與模擬引擎（SPEC 第 6–13 節）

| 項目 | 狀態 | 證據（檔案／函式／測試） | 說明與已知風險 |
| :--- | :---: | :--- | :--- |
| 6 虛擬總資金 100,000 USD 與短／長線池隔離（30,000 / 70,000，6 個子帳戶） | 完成 | [config/config.yaml](config/config.yaml)、[src/engine/simulator.py](src/engine/simulator.py) `Ledger.compute_pool_equity` | 短線池 `S1`–`S3` 各 `$10,000`；長線池 `L1`–`L3` 各約 `$23,333`。 |
| 7 全套風控規則（單筆、總風險、日／週虧損、8% 減半、12% 暫停與 `control.json` 解除、保證金 ≤ 30%、群組上限、子帳戶共用部位控管） | 完成 | [src/risk/manager.py](src/risk/manager.py) `RiskManager`、[src/strategies/six_strategies.py](src/strategies/six_strategies.py) `allocate_subaccount_position`、[test_risk.py](tests/test_risk.py)、[test_six_strategies.py](tests/test_six_strategies.py) | 實盤模擬與組合回測皆強制執行相同子帳戶風險上限。 |
| 8–9 統一單筆全倉位計算、成本模型、止盈止損與 1R/2R 移動止損 | 完成 | [src/strategies/six_strategies.py](src/strategies/six_strategies.py) `allocate_subaccount_position`、[src/engine/simulator.py](src/engine/simulator.py)、[test_engine_replay.py](tests/test_engine_replay.py) | 單筆進場 100% 計畫名目部位，無分階段補倉。 |
| 10 統一單一模擬帳本架構（取代舊多組帳本比較） | 完成 | [src/engine/simulator.py](src/engine/simulator.py)、[src/engine/analytics.py](src/engine/analytics.py)、[tests/test_engine_replay.py](tests/test_engine_replay.py) | 所有已結束、進行中與待進場單皆收斂至單一帳本 `ledger`。 |
| 11 統一 6 個策略（S1–S3 短線、L1–L3 長線）進出場與共用風控 | 完成 | [src/strategies/six_strategies.py](src/strategies/six_strategies.py)、[test_six_strategies.py](tests/test_six_strategies.py)、[test_scan_job.py](tests/test_scan_job.py) | 回測與即時掃描執行完全相同的 `decide_strategy_step`、`decide_l2_rotation_step` 與 `allocate_subaccount_position`。 |
| 12 組合級 IS/OOS 歷史回測（5+ 年資料、Binance 月度壓縮檔快取、含全成本、嚴格 OOS 門檻） | 完成 | [src/backtest/screener.py](src/backtest/screener.py)、[src/jobs/weekly.py](src/jobs/weekly.py)、[src/jobs/daily.py](src/jobs/daily.py)、[test_six_strategies.py](tests/test_six_strategies.py) | 嚴格套用 5 年、OOS >= 30、期望值 > 0、PF > 1.3、MDD < 25%，絕不放寬；第一階段回測驗證完成後才進入第二階段接單。 |
| 13 模擬引擎、槓桿／爆倉距離 ≤ 50%、休市跳空開盤價成交與完整單元測試 | 完成 | [src/engine/simulator.py](src/engine/simulator.py)、[tests/](tests/) | 涵蓋回測即時一致性、防 OOS 洩漏、權益曲線 > 0 與回撤 < 100%。 |
