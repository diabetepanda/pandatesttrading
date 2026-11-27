# 最終補強驗證（final_check）：資料來源、假設與已知限制

本資料夾收錄「普通人該怎麼投資」系列研究的最終邏輯補強測試（共 8 項）。所有判斷門檻均於執行回測前先寫死於 [criteria.md](criteria.md) 並提交至 Git（commit `d121bf4`），事後未做修改。

---

## 一、執行方式

```bash
# 1. 若尚未快取新標的與 Shiller 資料（免 API key）
.venv/bin/python research/final_check/fetch_data.py

# 2. 執行全部 8 項測試，輸出 results.json
.venv/bin/python research/final_check/run_final_check.py

# 3. 繪製 8 張繁體中文 PNG 圖表
.venv/bin/python research/final_check/charts.py

# 4. 執行單元測試
.venv/bin/python -m pytest tests/test_final_check.py -v
```

---

## 二、資料來源與查詢日期（查詢日：2026-11-27）

| 資料項目 | 來源與公開網址 | 覆蓋期間 | 用途 |
| :--- | :--- | :--- | :--- |
| `0050.TW`、`00631L.TW`、`^TWII`、`SPY`、`QQQ`、`TWD=X`、`^IRX`、`^SP500TR`、`^GSPC` | 重用既有快取（`research/dca_allocation_grid/cache/`、`research/peak_recovery/cache/`、`research/robustness_check/cache/`）與 `robustness_check/engine.py`、`dca_allocation_grid` 之清洗邏輯 | `1997-07-02 ~ 2026-11-25`（美股自 `1990-01`） | 測試 1、2、3、4、5、7、8 |
| `CSPX.L`（愛爾蘭註冊累積型 S&P 500 ETF，美元計價） | Yahoo Finance（`research/final_check/cache/final_check_yf.pkl`）與 BlackRock iShares 官網：https://www.ishares.com/uk/individual/en/products/253743/ishares-sp-500-b-ucits-etf-acc-fund | `2010-09-15 ~ 2026-11-25`（上市前由 SPY 調整 15% 預扣稅與 0.07% 費率推算） | 測試 2、4、5、7、8 |
| `VOO`（美國註冊 Vanguard S&P 500 ETF） | Yahoo Finance 與 Vanguard 官網：https://investor.vanguard.com/investment-products/etfs/profile/voo | `2010-09-09 ~ 2026-11-25` | 測試 2 |
| `SPY`（美國註冊 SPDR S&P 500 ETF Trust） | Yahoo Finance 與 SSGA 官網：https://www.ssga.com/us/en/intermediary/etfs/spdr-sp-500-etf-trust-spy | `1993-01-29 ~ 2026-11-25` | 測試 2 |
| `SHY`（1–3 年美債 ETF）、`IEF`（7–10 年美債 ETF） | Yahoo Finance 與 iShares 官網：https://www.ishares.com/us/products/239452/ishares-13-year-treasury-bond-etf 、https://www.ishares.com/us/products/239456/ishares-710-year-treasury-bond-etf | `2002-07-30 ~ 2026-11-25` | 測試 3 |
| `VGIT`（3–10 年中期美債 ETF） | Yahoo Finance 與 Vanguard 官網：https://investor.vanguard.com/investment-products/etfs/profile/vgit | `2009-11-23 ~ 2026-11-25` | 測試 3、4、5、7、8 |
| `^TNX`（10 年期美債殖利率）、`^FVX`（5 年期美債殖利率） | Yahoo Finance | `1962-01-02 ~ 2026-11-25` | 測試 3 上市前債券推算、測試 6 尾端銜接 |
| `0050` 與 `006208` 現行總費用率 | 元大投信官網：https://www.yuantaetfs.com/product/detail/0050/Basic_information ；富邦投信官網：https://www.fsit.com.tw/ | 2025–2026 現行級距費率 | 測試 2 費用核對 |
| Robert Shiller 長歷史月資料（S&P 500、股息、美國 CPI、10 年公債殖利率） | Yale 官網原始檔：http://www.econ.yale.edu/~shiller/data/ie_data.xls ；整理版 CSV 鏡像：https://raw.githubusercontent.com/datasets/s-and-p-500/main/data/data.csv | 核心完整段 `1871-01 ~ 2023-06`（1,830 個月） | 測試 4、5、6 |
| 美國消費者物價指數 `CPIAUCSL`（銜接 2023-07～2026-10） | St. Louis Fed FRED 公開免金鑰 CSV：https://fred.stlouisfed.org/graph/fredgraph.csv?id=CPIAUCSL | `2023-07 ~ 2026-10`（與 `^SP500TR`、`^TNX` 銜接 Shiller 尾端） | 測試 6（完整涵蓋至 2026-10） |
| 美國非居民聯邦遺產稅級距（Form 706-NA） | IRS Form 706-NA 說明與 IRC §§ 2001(c), 2101, 2102(b)(1)：https://www.irs.gov/forms-pubs/about-form-706-na | 現行聯邦稅法（USD 13,000 統一抵減額＝等值 USD 60,000 免稅額，累進稅率 18%–40%） | 測試 2 |

---

## 三、主要假設清單

1. **資料清洗**：直接沿用 `research/dca_allocation_grid/run_dca_allocation_grid.py` 與 `research/robustness_check/run_robustness.py`：
   - `0050.TW` 修正 2025 年 1 拆 4 斷層、補齊 2009–2014 官方配息、剔除 2009-01-02～2009-03-09 錯價；2009-03-10 前以 `^TWII + 3.0%/年` 殖利率銜接至 1997-07-02。
   - `00631L.TW` 剔除 2015-08-24 前錯價與分割斷層，2015-08-25 前以 `2 × 0050 含息 - 1.16%/年` 模擬。
   - `TWD=X` 清除 2011-10-25、2014-12-31 與 2015–2016 壞點；2004-03-24 前延用 32.023。
   - `^IRX` 負值歸零處理。
2. **交易摩擦與稅賦**：
   - 台股再平衡／濾網切換：賣出手續費 `0.1425%` + ETF 證交稅 `0.1%`（合計 `0.2425%`）；買入手續費 `0.1425%`。
   - 美股／英股交易：換匯價差基準 `0.1%`、券商手續費 `0.1%`（主模擬採免低消管道，傳統複委託 USD 15 低消與 0.3% 換匯價差已於 `robustness_check` 第 4 關量化）。
   - 股息預扣稅：美國註冊股票 ETF（`SPY`/`VOO`）`30%`；愛爾蘭註冊累積型 `CSPX` 基金層級 `15%`（投資人層級無額外預扣）。
   - 美國公債 ETF 配息稅：主模擬採合格利息收入（QII）免預扣 `0%`（主流海外券商如 IBKR 次年自動退稅或免扣），同時加跑 `30%` 預扣稅壓力測試（部分不辦理 QII 退稅之複委託）。
3. **債券上市前推算公式**：
   - 採標準修正存續期與凸性公式：$r_t = \frac{y_{t-1}}{252} - D \cdot \Delta y_t + \frac{1}{2} C (\Delta y_t)^2 - \frac{\text{ER}}{252}$。
   - `SHY`（1–3 年，$D=1.9, C=4.5, \text{ER}=0.15\%$）以 `0.65 × ^IRX + 0.35 × ^FVX` 推算；`VGIT`（3–10 年，$D=5.0, C=30.0, \text{ER}=0.03\%$）以 `^FVX` 推算；`IEF`（7–10 年，$D=7.5, C=65.0, \text{ER}=0.15\%$）以 `^TNX` 推算。
   - 重疊實測檢驗：月報酬相關係數分別為 `SHY 0.8356`、`VGIT 0.9875`、`IEF 0.9895`；推算版年化報酬較真實 ETF 保守 `0.26%～0.68%`（無灌水）。
4. **美國遺產稅**：依 IRS Form 706-NA，非美居民美國境內資產超過 USD 60,000（統一抵減額 USD 13,000）適用 18%–40% 累進級距。同時試算「僅美股 ETF 課稅」與「美股 ETF + 美國註冊公債 ETF（如 VGIT）一併視為美國境內資產課稅」兩種情境，以匯率 `32.0` 換算台幣。

---

## 四、已知限制與代理資料揭露

1. **長歷史台股代理資料（測試 4、5、6）**：1871–1997 年無台灣股市歷史日線／月線，Shiller 長歷史模擬（1871–2026）期間，台股部位以美國 S&P 500 含息報酬（扣除 0.32%/年估計愛爾蘭 ETF 費用與預扣稅）代理，債券以 10 年期美國公債殖利率 `GS10` 推算，且未含台幣對美元長歷史匯率波動。
2. **Shiller 尾端銜接（2023-07～2026-10）**：公開 Shiller CSV 的股息與 CPI 欄位更新至 `2023-06`，`2023-07～2026-10` 共 40 個月以 Yahoo `^SP500TR`、`^TNX` 與 FRED `CPIAUCSL`（依 2023-06 基準等比例錨定）銜接。
3. **2000 與 2008 退休路徑截斷（測試 6）**：截至 2026-10，2000-08 退休路徑已滿 `26 年`、2008-01 退休路徑已滿 `18 年`，尚未走完完整 30 年，報告與圖表均明確標註實測年數。
4. **英股複委託實務費率**：永豐金證券與富邦證券確認支援倫敦證交所美元計價 `CSPX`，但各券商英股複委託的實際折讓費率與最低手續費（常為議價制且會隨活動變動），須投資人開戶時自行向營業員確認，本研究未假設特定單一券商之專案優惠價。
5. **台灣 0050 / 006208 實時總費用率**：兩檔於 2025 年改採規模遞減級距式經理費與保管費，實際年度總費用率隨每日基金規模浮動，報告所列 `0.22%` 與 `0.23%` 為 2025–2026 公開揭露估算水準，正式數值仍以投信公會年度決算資料為準。
6. **美國註冊債券 ETF 的遺產稅盲點**：依美國稅法，直接持有美國公債通常適用投資組合債務免稅（Portfolio Debt Exemption），但**透過美國註冊 ETF（如 `VGIT`、`SHY`、`IEF`）間接持有時，ETF 股份本身屬於美國公司股權，仍計入美國遺產稅課稅基礎**。報告已於測試 2 同步揭露此差異並標明愛爾蘭註冊美債 ETF（例如 `IDTM`/`DTLA`/`IBTA` 等 UCITS 債券 ETF，本輪未單獨下載回測價格，明標「未回測」）。
