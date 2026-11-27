(function (global) {
  "use strict";

  const STORAGE_KEY = "signal_desk_glossary_enabled";
  const MAX_TERMS_PER_BLOCK = 3;

  const GLOSSARY_TERMS = [
    // ==================== 央行（12） ====================
    {
      id: "fed",
      term: "聯準會",
      en: "Federal Reserve (Fed)",
      category: "央行",
      aliases: ["聯準會"],
      plain: "美國的中央銀行，負責制定美元基準利率與管理銀行體系流動性，政策影響全球股匯債與加密市場。",
      trading_meaning: "聯準會態度偏緊或偏鬆，往往直接決定市場中長線資金成本與風險資產估值上限。",
    },
    {
      id: "fomc",
      term: "FOMC",
      en: "Federal Open Market Committee",
      category: "央行",
      aliases: ["FOMC", "聯邦公開市場委員會"],
      plain: "聯準會內部的利率決策會議，每年召開 8 次，投票決定是否升息、降息或維持利率不變。",
      trading_meaning: "FOMC 決議公布前後（台北時間凌晨 02:00/03:00）通常伴隨劇烈跳空與假突破，需嚴格控管槓桿。",
    },
    {
      id: "hawkish",
      term: "鷹派",
      en: "Hawkish",
      category: "央行",
      aliases: ["鷹派", "偏鷹"],
      plain: "央行官員優先擔心通膨過高，傾向升息、延後降息或縮表來壓抑物價與過熱經濟的立場。",
      trading_meaning: "市場出現偏鷹訊號時，美債殖利率與美元容易走強，對科技股與加密貨幣通常偏短空壓力。",
    },
    {
      id: "dovish",
      term: "鴿派",
      en: "Dovish",
      category: "央行",
      aliases: ["鴿派", "偏鴿"],
      plain: "央行官員優先關注就業與經濟成長，傾向降息或維持寬鬆環境以支撐景氣的立場。",
      trading_meaning: "偏鴿言論通常壓低美債殖利率與美元，有利風險偏好回升與高估值成長股反彈。",
    },
    {
      id: "rate_move",
      term: "升息／降息",
      en: "Rate Hike / Rate Cut",
      category: "央行",
      aliases: ["升息／降息", "升息", "降息"],
      plain: "聯準會調高（升息）或調低（降息）基準利率的動作，用來調整借貸成本與市場熱度。",
      trading_meaning: "升息循環收緊市場銀根、降息循環釋放流動性，但市場往往會提前幾個月透過期貨預先定價。",
    },
    {
      id: "bps",
      term: "碼（基點）",
      en: "25 bps / Basis Point",
      category: "央行",
      aliases: ["碼（基點）", "1 碼", "2 碼", "半碼", "基點", "bp"],
      plain: "台灣常稱 0.25 個百分點為「1 碼」，金融市場也用 1 個基點（1 bp = 0.01%）表示，因此 1 碼 = 25 個基點。",
      trading_meaning: "當市場討論升 1 碼或 2 碼時，實質是在比較資金成本上調 0.25% 或 0.50% 的幅度差異。",
    },
    {
      id: "dot_plot",
      term: "點陣圖",
      en: "Dot Plot",
      category: "央行",
      aliases: ["點陣圖"],
      plain: "聯準會在每季（3、6、9、12 月）FOMC 會議後公布的預測圖，每個點代表一位官員對未來幾年利率落點的預估。",
      trading_meaning: "點陣圖中位數若上移代表官員預期高利率更久，常比當次利率決議本身更影響美債與股匯走勢。",
    },
    {
      id: "beige_book",
      term: "褐皮書",
      en: "Beige Book",
      category: "央行",
      aliases: ["褐皮書", "Beige Book"],
      plain: "聯準會在每次 FOMC 會議前兩週發布的報告，彙整美國 12 個聯儲轄區的企業訪談、物價與就業近況。",
      trading_meaning: "可提前觀察聯準會官員在會議前看到的實體景氣與薪資壓力，作為研判會議基調的線索。",
    },
    {
      id: "fomc_minutes",
      term: "會議紀要",
      en: "FOMC Minutes",
      category: "央行",
      aliases: ["會議紀要", "FOMC Minutes"],
      plain: "FOMC 會後約三週公布的詳細討論紀錄，揭示委員們對通膨、就業與下次是否調整利率的分歧與共識。",
      trading_meaning: "若紀要顯示多數官員比記者會發言更鷹派或更謹慎，容易引發債市與美元在隔夜時段補反應。",
    },
    {
      id: "blackout_period",
      term: "靜默期",
      en: "FOMC Blackout Period",
      category: "央行",
      aliases: ["靜默期"],
      plain: "FOMC 會議前約十天（從會前第二個週六起至會後隔日），聯準會官員禁止公開談論貨幣政策立場。",
      trading_meaning: "靜默期內少了官員放風聲，市場焦點會完全集中於同期公布的 CPI、就業等硬數據。",
    },
    {
      id: "fedwatch",
      term: "FedWatch",
      en: "CME FedWatch Tool",
      category: "央行",
      aliases: ["FedWatch"],
      plain: "芝商所（CME）利用 30 天期聯邦基金期貨價格反推市場對未來 FOMC 會議升息、降息或維持機率的模型。",
      trading_meaning: "用來確認市場是否已充分定價某項利率結果；機率接近 100% 時，決議本身反而較難出現單邊大行情。",
    },
    {
      id: "fed_funds_rate",
      term: "聯邦基金利率",
      en: "Federal Funds Rate",
      category: "央行",
      aliases: ["聯邦基金利率"],
      plain: "美國銀行之間隔夜拆借準備金的基準利率，由 FOMC 設定目標區間（例如 3.75%–4.00%）。",
      trading_meaning: "全市場最底層的無風險利率基準，牽動美債殖利率、美元匯率與各類資產估值模型。",
    },

    // ==================== 通膨（6） ====================
    {
      id: "cpi",
      term: "CPI",
      en: "Consumer Price Index",
      category: "通膨",
      aliases: ["CPI"],
      plain: "消費者物價指數，衡量一般家庭購買食衣住行與能源等一籃子商品與服務的平均價格漲幅。",
      trading_meaning: "每月市場最關注的通膨數據之一，高於預期容易推升殖利率並壓抑股市與幣圈。",
    },
    {
      id: "core_cpi",
      term: "核心 CPI",
      en: "Core CPI",
      category: "通膨",
      aliases: ["核心 CPI", "Core CPI"],
      plain: "剔除波動較大的食品與能源價格後的 CPI，主要反映房租、醫療與各類服務業的內生通膨壓力。",
      trading_meaning: "聯準會比整體 CPI 更看重核心 CPI 的黏性，若環比連續偏高會明顯壓縮降息空間。",
    },
    {
      id: "pce",
      term: "PCE",
      en: "Personal Consumption Expenditures Price Index",
      category: "通膨",
      aliases: ["PCE"],
      plain: "個人消費支出物價指數，比 CPI 涵蓋更廣，並會隨消費者替代購買行為動態調整權重。",
      trading_meaning: "聯準會官方設定 2% 通膨目標所採用的基準指數，長期趨勢直接左右政策週期。",
    },
    {
      id: "core_pce",
      term: "核心 PCE",
      en: "Core PCE",
      category: "通膨",
      aliases: ["核心 PCE", "Core PCE"],
      plain: "剔除食品與能源的個人消費支出物價指數，是聯準會內部評估潛在通膨最核心的尺標。",
      trading_meaning: "若核心 PCE 環比持續高於 +0.2%，意味年化通膨仍高於 2% 目標，市場難以期待快速寬鬆。",
    },
    {
      id: "ppi",
      term: "PPI",
      en: "Producer Price Index",
      category: "通膨",
      aliases: ["PPI"],
      plain: "生產者物價指數，衡量製造商與批發端在銷售商品與服務時所面對的出廠成本變化。",
      trading_meaning: "常作為 CPI 與 PCE 的上游領先參考，部分醫療與金融分項也會直接計入核心 PCE。",
    },
    {
      id: "mom_yoy",
      term: "環比／同比",
      en: "MoM / YoY",
      category: "通膨",
      aliases: ["環比", "同比"],
      plain: "環比（MoM）是跟上個月比、反映最新短期動能；同比（YoY）是跟去年同期比、反映全年累積漲幅。",
      trading_meaning: "轉折初期看環比最敏銳，確認長期通膨是否回到 2% 目標則看同比。",
    },

    // ==================== 就業（5） ====================
    {
      id: "nfp",
      term: "非農就業",
      en: "Nonfarm Payrolls (NFP)",
      category: "就業",
      aliases: ["非農就業", "非農"],
      plain: "美國每月新增（或減少）的非農業受薪就業人數，由勞工統計局在每月第一個週五公布。",
      trading_meaning: "高利率下「溫和放緩」最利多股市；過熱代表難降息，轉負則可能引發經濟衰退避險。",
    },
    {
      id: "unrate",
      term: "失業率",
      en: "Unemployment Rate",
      category: "就業",
      aliases: ["失業率"],
      plain: "有工作意願且正在找工作但尚未就業的人口占總勞動力的百分比，與非農就業同日公布。",
      trading_meaning: "失業率低代表勞動市場偏緊；緩步上升有助壓低薪資通膨，但若急升常被視為衰退警訊。",
    },
    {
      id: "icsa",
      term: "初領失業金",
      en: "Initial Jobless Claims",
      category: "就業",
      aliases: ["初領失業金", "初請失業金"],
      plain: "每週四公布前一週首次申請失業救濟金的人數，是目前頻率最高的美國裁員與就業溫度計。",
      trading_meaning: "人數溫和走高顯示勞動市場降溫；若連續數週跳升，常提前預警後續非農數據轉弱。",
    },
    {
      id: "jolts",
      term: "JOLTS",
      en: "Job Openings and Labor Turnover Survey",
      category: "就業",
      aliases: ["JOLTS", "職位空缺"],
      plain: "美國勞工統計局每月公布的職位空缺與離職調查，反映企業開缺搶人的需求強度。",
      trading_meaning: "職位空缺明顯下降代表勞動供需趨於平衡、有利通膨回落；若居高不下則聯準會難轉鴿。",
    },
    {
      id: "pmi",
      term: "PMI",
      en: "Purchasing Managers' Index",
      category: "景氣",
      aliases: ["PMI"],
      plain: "採購經理人指數，以 50 為景氣榮枯線：高於 50 代表產業處於擴張、低於 50 代表收縮。",
      trading_meaning: "讀數由 50 以下翻回 50 以上常對應景氣復甦初期，但同時要留意物價分項是否升溫。",
    },

    // ==================== 景氣（4） ====================
    {
      id: "ism_mfg",
      term: "ISM 製造業",
      en: "ISM Manufacturing PMI",
      category: "景氣",
      aliases: ["ISM 製造業", "ISM 製造業 PMI"],
      plain: "美國供應管理協會每月第一個工作日發布的製造業景氣調查，涵蓋新訂單、生產、就業與支付價格。",
      trading_meaning: "雖製造業占 GDP 比重不如服務業，但對庫存週期、半導體與工業股極具領先指標意義。",
    },
    {
      id: "ism_svc",
      term: "ISM 服務業",
      en: "ISM Services PMI",
      category: "景氣",
      aliases: ["ISM 服務業", "ISM 服務業 PMI"],
      plain: "美國供應管理協會每月第三個工作日發布的服務業調查，涵蓋金融、餐飲、運輸與科技服務業。",
      trading_meaning: "服務業占美國經濟產出大宗，若讀數與物價分項維持高檔，核心服務通膨不易快速降溫。",
    },
    {
      id: "retail_sales",
      term: "零售銷售",
      en: "Advance Retail Sales",
      category: "景氣",
      aliases: ["零售銷售"],
      plain: "衡量美國零售商與餐飲業每月總銷售額變化，是觀察美國民間消費力道最直接的月度數據。",
      trading_meaning: "民間消費占美國 GDP 約七成；數據穩健支撐企業獲利，過熱則可能強化高利率維持預期。",
    },
    {
      id: "gdp",
      term: "GDP",
      en: "Gross Domestic Product",
      category: "景氣",
      aliases: ["GDP"],
      plain: "國內生產毛額，衡量一國在特定期間內生產的所有最終商品與勞務總值，美國採季增年化率呈現。",
      trading_meaning: "反映經濟整體健康度；單季過強可能支撐高利率，連續兩季轉負則容易引發衰退討論。",
    },

    // ==================== 市場（11） ====================
    {
      id: "yield",
      term: "殖利率",
      en: "Treasury Yield",
      category: "市場",
      aliases: ["殖利率", "美債殖利率"],
      plain: "買進並持有美國公債至到期的年化報酬率；債券價格下跌時殖利率會上升，債價上漲時殖利率下降。",
      trading_meaning: "10 年期美債殖利率是全球資產折現錨，走高時科技股與黃金通常承受估值壓力。",
    },
    {
      id: "yield_curve_inversion",
      term: "殖利率曲線倒掛",
      en: "Yield Curve Inversion",
      category: "市場",
      aliases: ["殖利率曲線倒掛", "殖利率倒掛", "曲線倒掛"],
      plain: "正常情況下長天期利率應高於短天期；當 2 年期殖利率高於 10 年期時稱為倒掛，常出現在緊縮末期。",
      trading_meaning: "歷史上倒掛後往往預告未來經濟放緩風險，但市場真正的轉折常發生在「重新轉正」時。",
    },
    {
      id: "real_rate",
      term: "實質利率",
      en: "Real Interest Rate",
      category: "市場",
      aliases: ["實質利率"],
      plain: "名目利率（如美債殖利率）減去預期通膨率後的真實資金報酬，代表扣除物價侵蝕後的利息回報。",
      trading_meaning: "黃金與比特幣不生息，因此實質利率上升時往往承壓、下降時容易獲得上行動能。",
    },
    {
      id: "dxy",
      term: "美元指數",
      en: "US Dollar Index (DXY)",
      category: "市場",
      aliases: ["美元指數", "DXY"],
      plain: "追蹤美元對歐元、日圓、英鎊等六大主要貨幣的綜合匯率強弱指數。",
      trading_meaning: "DXY 強升通常意味全球美元流動性收緊，對台股、新興市場與加密貨幣偏逆風。",
    },
    {
      id: "twd_fx",
      term: "台幣升值／貶值",
      en: "USD/TWD Appreciation / Depreciation",
      category: "市場",
      aliases: ["台幣升值／貶值", "台幣升值", "台幣貶值"],
      plain: "美元兌台幣匯率數字變小代表台幣升值（換 1 美元需較少台幣），數字變大代表台幣貶值。",
      trading_meaning: "台幣升值常伴隨外資匯入買超台股；台幣連續貶值則要留意權值股遭外資調節壓力。",
    },
    {
      id: "foreign_flow",
      term: "外資買超／賣超",
      en: "Foreign Institutional Net Buy/Sell",
      category: "市場",
      aliases: ["外資買超／賣超", "外資買超", "外資賣超", "外資匯入", "外資匯出"],
      plain: "境外機構投資人在台股集中市場買進金額與賣出金額的差額，常與台幣匯率及美股科技股連動。",
      trading_meaning: "台股大型權值股主要由外資定價，連續買超或賣超往往決定指數中期趨勢方向。",
    },
    {
      id: "etf_inflow",
      term: "ETF 淨流入",
      en: "ETF Net Inflow",
      category: "市場",
      aliases: ["ETF 淨流入", "ETF淨流入"],
      plain: "單日或單週申購某檔 ETF（如美股現貨比特幣 ETF）的新增資金扣除贖回後的淨額。",
      trading_meaning: "持續大額淨流入代表機構與傳統資金穩定進場承接，連續淨流出則反映買盤轉弱。",
    },
    {
      id: "bull_bear",
      term: "利多／利空",
      en: "Bullish / Bearish",
      category: "市場",
      aliases: ["利多／利空", "偏利多", "偏利空", "利多", "利空"],
      plain: "利多指有助資產價格上漲的消息或數據；利空指可能壓抑價格、引發拋售的負面因素。",
      trading_meaning: "同一數據對不同資產影響可能相反，例如就業過熱對美元偏利多，對想等降息的股市卻偏利空。",
    },
    {
      id: "surprise",
      term: "意外值（實際減預期）",
      en: "Data Surprise (Actual - Consensus)",
      category: "市場",
      aliases: ["意外值（實際減預期）", "意外值"],
      plain: "官方實際公布的數據減去市場共識預期值；正值代表高於預期，負值代表低於預期。",
      trading_meaning: "金融市場定價的是「預期差」，數據好壞本身不如是否超出市場預期來得重要。",
    },
    {
      id: "risk_sentiment",
      term: "風險偏好（risk-on／risk-off）",
      en: "Risk-On / Risk-Off",
      category: "市場",
      aliases: ["風險偏好（risk-on／risk-off）", "風險偏好", "risk-on", "risk-off"],
      plain: "Risk-on 代表資金願意承擔波動買進股票與加密貨幣；Risk-off 代表資金轉進美債、美元與黃金避險。",
      trading_meaning: "辨識目前市場處於哪種模式，能避免在避險模式時過度追高高 Beta 標的。",
    },
    {
      id: "liquidity",
      term: "流動性",
      en: "Market Liquidity",
      category: "市場",
      aliases: ["流動性"],
      plain: "兩層含義：一是市場上可自由買賣的熱錢多寡（宏觀流動性），二是單一標的是否容易大額成交不滑價。",
      trading_meaning: "宏觀流動性寬鬆是牛市燃料；交投清淡或流動性枯竭時，少量大單就容易引發劇烈插針。",
    },

    // ==================== 加密（6） ====================
    {
      id: "funding_rate",
      term: "資金費率",
      en: "Funding Rate",
      category: "加密",
      aliases: ["資金費率"],
      plain: "永續合約為讓合約價貼近現貨價而設計的週期性費用；正費率代表做多方付錢給做空方，負費率相反。",
      trading_meaning: "極端正費率代表多頭槓桿擁擠，若行情走不動容易引發連鎖回調與多殺多。",
    },
    {
      id: "open_interest",
      term: "未平倉量（OI）",
      en: "Open Interest (OI)",
      category: "加密",
      aliases: ["未平倉量（OI）", "未平倉量", "OI"],
      plain: "市場上目前尚未結算或平倉的期貨／選擇權合約總口數或總名目價值，用來衡量場內槓桿累積程度。",
      trading_meaning: "價格急漲同時 OI 暴增代表新槓桿湧入，後續若反轉常伴隨大幅清算波動。",
    },
    {
      id: "liquidation",
      term: "爆倉",
      en: "Liquidation",
      category: "加密",
      aliases: ["爆倉", "清算"],
      plain: "槓桿交易者的保證金低於維持門檻時，系統強制以市價平倉部位的機制。",
      trading_meaning: "密集止損與爆倉點位常成為短線磁吸區，大額爆倉出清後反而常見短線止跌反彈。",
    },
    {
      id: "stablecoin",
      term: "穩定幣",
      en: "Stablecoin (USDT / USDC)",
      category: "加密",
      aliases: ["穩定幣"],
      plain: "價格錨定法定貨幣（多為 1 美元）的加密貨幣，如 USDT 與 USDC，是幣圈交易與避險的核心計價工具。",
      trading_meaning: "穩定幣總市值與交易所流入增加，通常代表場外準備買幣的美元子彈正在增加。",
    },
    {
      id: "tvl",
      term: "TVL",
      en: "Total Value Locked",
      category: "加密",
      aliases: ["TVL"],
      plain: "總鎖倉價值，衡量去中心化金融（DeFi）協議中目前質押與存入的加密資產總美元規模。",
      trading_meaning: "TVL 穩步上升且非單靠幣價漲幅推升，通常代表該公鏈或協議的真實資金黏著度提升。",
    },
    {
      id: "wti",
      term: "WTI 原油",
      en: "West Texas Intermediate Crude",
      category: "市場",
      aliases: ["WTI 原油", "原油 (WTI)"],
      plain: "美國西德州中級原油期貨，是北美與全球最重要的原油定價基準之一。",
      trading_meaning: "油價急漲會直接墊高運輸與製造能源成本，進而影響 CPI 預期與央行利率決策。",
    },
    {
      id: "hsi",
      term: "恒生指數",
      en: "Hang Seng Index (HSI)",
      category: "市場",
      aliases: ["恒生指數", "恆生指數", "恒指", "恆指", "HSI"],
      plain: "香港股市主要指數，成分股以大型陸資與香港企業為主，可以看出中國經濟與亞洲資金的情緒。",
      trading_meaning: "恒生走強通常代表亞洲風險偏好回升，對台股與亞股氣氛偏正面；轉弱時則需留意風險情緒降溫。",
    },
    {
      id: "foreign_zscore",
      term: "外資買賣超 z-score",
      en: "Foreign Net Flow 60D Z-Score",
      category: "市場",
      aliases: ["外資買賣超 z-score", "外資現貨買賣超 60 日 z-score", "外資買賣超 60日z分數"],
      plain: "把外資今天在台股現貨買超或賣超的金額，拿來跟過去 60 個交易日的平均水準比較，換算成「偏離幾個標準差」。這樣才不會因為台股市值變大而把十年前的 100 億跟現在的 100 億混在一起。",
      trading_meaning: "當 z > +1.5 代表外資出現近一季罕見的大買，歷史上隔天（僅限 1 日）上漲機率略高於平常；但統計證據偏弱，只能當輔助參考，不能當買賣訊號。",
    },
    {
      id: "txf_net_oi",
      term: "台指期淨未平倉",
      en: "Foreign TXF Net Open Interest",
      category: "市場",
      aliases: ["台指期淨未平倉", "外資台指期淨未平倉"],
      plain: "外資在台指期貨留倉的多單口數減去空單口數後的淨額。因為外資常常長期留幾萬口空單做現貨避險，只看總口數很容易誤判，看「跟昨天相比的單日增減變化」才準。",
      trading_meaning: "當單日變化 60 日 z-score < -1.5 且不是每月第三個星期三結算日時，代表外資突然大舉加空或砍多，隔天下跌風險升高；同樣只看隔天且證據偏弱，僅作風險提示。",
    },
    {
      id: "xirr",
      term: "XIRR",
      en: "Extended Internal Rate of Return",
      category: "市場",
      aliases: ["XIRR", "年化報酬（XIRR）"],
      plain: "針對「定期定額」或「不定期投入」計算的真實年化報酬率。因為第一筆錢滾了十幾年、最後一筆錢只滾了幾天，XIRR 會把每筆資金實際待在市場的時間精算後，換算成相當於每年幾 % 的複利。",
      trading_meaning: "比較不同定期定額配置時，不能只看總報酬率，必須看 XIRR 才能公平衡量資金的時間效率。",
    },
    {
      id: "mdd",
      term: "MDD",
      en: "Maximum Drawdown",
      category: "市場",
      aliases: ["MDD", "最大回撤"],
      plain: "帳戶淨值從歷史曾經達到的最高點，一路往下跌到最慘谷底的最大跌幅百分比。例如 -50% 代表帳戶曾從 1,000 萬腰斬到剩 500 萬。",
      trading_meaning: "衡量策略或資產配置最痛的心理考驗；選配置前應先確認自己能承受的 MDD 上限，避免在谷底被迫停損或停扣。",
    },
    {
      id: "mar",
      term: "MAR",
      en: "MAR Ratio (Return / Max Drawdown)",
      category: "市場",
      aliases: ["MAR", "報酬回撤比"],
      plain: "年化報酬率除以最大回撤幅度的比值。數字越高，代表每承受 1% 的帳面最大縮水痛苦，能換到越多 % 的年化報酬。",
      trading_meaning: "用來衡量風險報酬性價比；同樣報酬下，MAR 越高的組合走勢越平順、越容易長期抱得住。",
    },
    {
      id: "effective_leverage",
      term: "有效槓桿",
      en: "Effective Leverage",
      category: "市場",
      aliases: ["有效槓桿", "有效槓桿倍數"],
      plain: "整個投資組合加權後的整體槓桿倍數。全部買 1 倍原型 ETF（如 0050、SPY、QQQ）就是 1.0x；一半 1 倍、一半 2 倍正二（如 00631L）就是 1.5x。",
      trading_meaning: "決定組合的波動放大程度與長期複利上限；實證顯示 1.2x～1.4x 溫和槓桿在長期定投中最容易兼顧報酬與心理承受度。",
    },
    {
      id: "rebalance",
      term: "再平衡",
      en: "Portfolio Rebalancing",
      category: "市場",
      aliases: ["再平衡", "定期再平衡"],
      plain: "每隔一段時間（如每年或每季）把漲多的部位賣出一部分、補進跌多或漲較慢的部位，讓各標的權重回到原本設定的比例。",
      trading_meaning: "強迫執行「高賣低買」並把槓桿倍數拉回安全區間；在台美股或原型＋槓桿混搭中，能有效收割波動紅利並壓低崩盤傷害。",
    },
    {
      id: "sma200_filter",
      term: "200 日線濾網",
      en: "200-Day Moving Average Filter",
      category: "市場",
      aliases: ["200 日線濾網", "200 日線 ±2% 濾網", "200MA 濾網"],
      plain: "以大盤過去 200 個交易日的平均價格（年線）作為多空分界，並加上 ±2% 緩衝減少假訊號。在本研究中，跌破年線 -2% 時僅把「新投入的資金」改買 1 倍原型 ETF，不賣出舊部位。",
      trading_meaning: "像幫槓桿配置買保險：在 2000、2008 等長期大空頭中能顯著壓低最大回撤，但在 V 型反彈牛市中會略微少賺一點報酬。",
    },
    {
      id: "recovery_time",
      term: "回本時間",
      en: "Time to Recovery",
      category: "市場",
      aliases: ["回本時間", "回本月數"],
      plain: "從買進高點下跌後，重新漲回原本高點（或定期定額帳戶市值重新大於等於總投入本金）所需要的時間。",
      trading_meaning: "跌幅越深，回本時間呈指數型拉長（跌 50% 要漲 100% 才回本）；定期定額與股息再投資能大幅縮短實際回本時間。",
    },
    {
      id: "underwater_period",
      term: "水下期間",
      en: "Underwater Period",
      category: "市場",
      aliases: ["水下期間", "總水下期", "水下期"],
      plain: "資產淨值或指數跌破歷史最高點之後，一直到重新創下新高為止的整段套牢時間（下跌期加上回升期）。",
      trading_meaning: "決定投資人心理煎熬長短的關鍵指標；泡沫破裂級崩盤的水下期間動輒數年到十多年，遠比單純看最大跌幅更考驗耐心。",
    },
    {
      id: "out_of_sample",
      term: "樣本外測試",
      en: "Out-of-Sample Testing",
      category: "市場",
      aliases: ["樣本外測試", "樣本外"],
      plain: "先用過去一段歷史資料挑出表現最好的策略，再拿去「當初沒看過的未來年份」實際考試，確認策略不是只在歷史上碰巧表現好。",
      trading_meaning: "防止過度擬合與事後諸葛的必備檢驗；過去十年的第一名在樣本外測試裡常常表現平平，分散配置反而更穩健。",
    },
    {
      id: "block_bootstrap",
      term: "Block Bootstrap",
      en: "Block Bootstrap Resampling",
      category: "市場",
      aliases: ["Block Bootstrap", "區塊重抽樣", "Stationary Bootstrap"],
      plain: "把歷史市場資料切成一段一段的連續時間區塊（例如 1 年或 3 年），隨機抽取拼貼成數千條新的未來路徑來做壓力測試。",
      trading_meaning: "比單純打散單日報酬更能保留股災時的連續波動聚集與匯率連動，用來補足歷史上極端熊市樣本不足的問題。",
    },
    {
      id: "confidence_interval",
      term: "信賴區間",
      en: "Confidence Interval",
      category: "市場",
      aliases: ["信賴區間", "90% 信賴區間"],
      plain: "用來表示統計估計值誤差範圍的區間。比較兩個組合的報酬差距時，若 90% 信賴區間橫跨 0%，代表兩者在統計上分不出高下。",
      trading_meaning: "提醒投資人不要迷信回測排行榜上零點幾個百分點的微小領先，挑配置應優先考量簡潔度與最壞情況下的存活能力。",
    },
    {
      id: "withdrawal_rate",
      term: "提領率",
      en: "Withdrawal Rate",
      category: "市場",
      aliases: ["提領率", "初始提領率", "4% 法則"],
      plain: "退休第一年從總資產提領出來當生活費的比例（例如 1,000 萬提領 4% 就是每年 40 萬），之後每年通常隨通膨微調。",
      trading_meaning: "提領率過高會大幅提高退休 30 年內把錢花光的破產風險；遇大跌時若能彈性調降至 3.5%，可顯著延長資產壽命。",
    },
    {
      id: "sequence_risk",
      term: "序列風險",
      en: "Sequence of Returns Risk",
      category: "市場",
      aliases: ["序列風險", "報酬順序風險"],
      plain: "同樣的平均報酬率，如果剛退休前幾年就遇到大股災，被迫在低點賣股換生活費，會導致本金永久縮水、提早破產的風險。",
      trading_meaning: "累積期不怕早年大跌（反而能撿便宜），但提領期最怕早年大跌，因此退休前應逐步加入短中期公債緩衝。",
    },
    {
      id: "emergency_fund",
      term: "緊急預備金",
      en: "Emergency Fund",
      category: "市場",
      aliases: ["緊急預備金"],
      plain: "獨立存放在活存或定存、不投入股市的 6～12 個月基本生活費，專門應付失業、生病或突發開支。",
      trading_meaning: "實證顯示股災時剛好失業被迫賣股的傷害是一般停扣的 3～5 倍；存滿緊急預備金是長期投資抱得住的第一道防線。",
    },
    {
      id: "ireland_ucits_etf",
      term: "愛爾蘭註冊 ETF",
      en: "Ireland-Domiciled UCITS ETF",
      category: "市場",
      aliases: ["愛爾蘭註冊 ETF", "愛爾蘭 ETF", "UCITS", "CSPX"],
      plain: "註冊於愛爾蘭、受歐盟 UCITS 法規監管並常於倫敦證交所掛牌的指數基金（如追蹤標普 500 的累積型 CSPX）。",
      trading_meaning: "受美愛租稅協定保護，美股配息預扣稅由 30% 降至 15%，且屬非美國境內資產、免課非美居民最高 40% 的美國遺產稅。",
    },
    {
      id: "us_estate_tax",
      term: "美國遺產稅",
      en: "US Non-Resident Alien Estate Tax",
      category: "市場",
      aliases: ["美國遺產稅", "遺產稅"],
      plain: "非美國稅務居民過世時，名下「美國境內資產」（含美國註冊 ETF 如 SPY、VOO）超過 6 萬美元免稅等值額度即須申報 Form 706-NA，稅率採 18%～40% 累進級距。",
      trading_meaning: "長期定期定額 20～30 年後美股部位常遠超過 6 萬美元，是否改用愛爾蘭註冊 ETF 會直接影響家人最終繼承金額。",
    },
    {
      id: "glidepath",
      term: "滑降期",
      en: "Retirement Glidepath",
      category: "市場",
      aliases: ["滑降期", "股債滑降"],
      plain: "在預定退休前 5 年（例如 55～59 歲），每年按固定比例調降股票比重、提高公債比重的過渡期。",
      trading_meaning: "避免在退休前夕因 100% 持股遇上大崩盤而大幅縮水，平順銜接至 60 歲後的提領配置。",
    },
    {
      id: "withdrawal_guardrail",
      term: "提領保護規則",
      en: "Withdrawal Guardrail",
      category: "市場",
      aliases: ["提領保護規則", "保護規則"],
      plain: "退休後遇市場大跌（例如 1 月資產低於退休當時超過 20%）時，暫時把當年度提領比例由 4% 調降為當下資產的 3.5%，等資產回升再恢復的防破產機制。",
      trading_meaning: "犧牲熊市年份的部分生活費彈性，換取大幅降低 30 年退休期把老本花光的機率。",
    },
    {
      id: "bond_duration",
      term: "存續期",
      en: "Bond Duration",
      category: "市場",
      aliases: ["存續期", "債券存續期"],
      plain: "衡量債券價格對利率變動敏感度的指標（單位為年）：存續期 5 年代表殖利率上升 1 個百分點時，債券價格約下跌 5%。",
      trading_meaning: "短債（如 SHY，存續期約 1.9 年）在升息年抗跌但息收較低；中債（如 VGIT，約 5 年）2022 跌幅較深但長期報酬與降息緩衝通常較佳。",
    },
    {
      id: "real_purchasing_power",
      term: "實質購買力",
      en: "Real Purchasing Power",
      category: "通膨",
      aliases: ["實質購買力"],
      plain: "把名目金額扣除物價指數（CPI）累積漲幅後，實際能買到的商品與服務水準。",
      trading_meaning: "退休 30 年若提領金額不隨通膨調整，名目看似沒破產，實質生活水準卻可能被物價吃掉一半以上。",
    },
  ];

  const TERM_BY_ID = {};
  const ALIAS_ENTRIES = [];
  GLOSSARY_TERMS.forEach((t) => {
    TERM_BY_ID[t.id] = t;
    const list = Array.isArray(t.aliases) && t.aliases.length ? t.aliases : [t.term];
    list.forEach((a) => {
      if (a) ALIAS_ENTRIES.push({ alias: String(a), id: t.id });
    });
  });
  // Sort longest-first so "核心 CPI" matches before "CPI", "殖利率曲線倒掛" before "殖利率"
  ALIAS_ENTRIES.sort((a, b) => b.alias.length - a.alias.length);

  const ASCII_WORD_RE = /^[A-Za-z0-9][A-Za-z0-9\-\s]*[A-Za-z0-9]$|^[A-Za-z0-9]+$/;

  function escapeRegex(s) {
    return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  }

  // Build single combined regex for fast scanning; ASCII aliases get word boundary guards
  const REGEX_PARTS = ALIAS_ENTRIES.map((e) => {
    const esc = escapeRegex(e.alias);
    if (ASCII_WORD_RE.test(e.alias)) {
      return `(?<![A-Za-z0-9_])${esc}(?![A-Za-z0-9_])`;
    }
    return esc;
  });
  const COMBINED_REGEX = new RegExp(`(${REGEX_PARTS.join("|")})`, "g");
  const ALIAS_TO_ID = {};
  ALIAS_ENTRIES.forEach((e) => {
    ALIAS_TO_ID[e.alias] = e.id;
  });

  function isEnabled() {
    try {
      const v = global.localStorage ? global.localStorage.getItem(STORAGE_KEY) : null;
      return v !== "0";
    } catch (_e) {
      return true;
    }
  }

  function setEnabled(flag) {
    try {
      if (global.localStorage) {
        global.localStorage.setItem(STORAGE_KEY, flag ? "1" : "0");
      }
    } catch (_e) {
      // ignore storage errors
    }
  }

  // Pure text segmentation helper (also testable without DOM in jsc)
  function findTermMatchesInText(text, usedIdsSet, maxRemaining) {
    if (!text || maxRemaining <= 0) return [];
    const trimmed = text.trim();
    if (!trimmed) return [];
    // Skip URLs and pure numbers
    if (/^https?:\/\//i.test(trimmed) || /^[\d\s.,%+\-–/():]+$/.test(trimmed)) return [];

    COMBINED_REGEX.lastIndex = 0;
    const matches = [];
    let m;
    while ((m = COMBINED_REGEX.exec(text)) !== null) {
      const matchedText = m[0];
      const termId = ALIAS_TO_ID[matchedText];
      if (!termId) continue;
      if (usedIdsSet && usedIdsSet.has(termId)) continue;
      // Avoid matching inside a URL embedded in prose
      const before = text.slice(Math.max(0, m.index - 12), m.index);
      if (/https?:\/\/\S*$/i.test(before)) continue;

      if (usedIdsSet) usedIdsSet.add(termId);
      matches.push({ start: m.index, end: m.index + matchedText.length, text: matchedText, id: termId });
      if (matches.length >= maxRemaining) break;
    }
    return matches;
  }

  const SKIP_TAGS = new Set(["INPUT", "TEXTAREA", "SELECT", "OPTION", "BUTTON", "CODE", "PRE", "SCRIPT", "STYLE", "SVG"]);

  function shouldSkipElement(el) {
    if (!el || el.nodeType !== 1) return true;
    if (SKIP_TAGS.has(el.tagName)) return true;
    if (el.classList && el.classList.contains("glossary-term")) return true;
    if (el.getAttribute && el.getAttribute("data-glossary-skip") != null) return true;
    return false;
  }

  function scanBlock(blockEl, maxTerms) {
    if (!blockEl || !isEnabled()) return 0;
    if (blockEl.getAttribute && blockEl.getAttribute("data-glossary-scanned") === "1") return 0;
    const cap = maxTerms != null ? maxTerms : MAX_TERMS_PER_BLOCK;
    const usedIds = new Set();
    let count = 0;

    // Collect text nodes first so DOM mutation doesn't confuse traversal
    const textNodes = [];
    function walk(node) {
      if (!node || count >= cap) return;
      if (node.nodeType === 3) {
        textNodes.push(node);
        return;
      }
      if (node.nodeType !== 1 || shouldSkipElement(node)) return;
      const children = node.childNodes || [];
      for (let i = 0; i < children.length; i++) {
        walk(children[i]);
      }
    }
    walk(blockEl);

    for (let i = 0; i < textNodes.length && count < cap; i++) {
      const tn = textNodes[i];
      const val = tn.nodeValue;
      if (!val || !val.trim()) continue;
      const hits = findTermMatchesInText(val, usedIds, cap - count);
      if (!hits.length) continue;

      const doc = blockEl.ownerDocument || document;
      const frag = doc.createDocumentFragment();
      let cursor = 0;
      for (let j = 0; j < hits.length; j++) {
        const h = hits[j];
        if (h.start > cursor) {
          frag.appendChild(doc.createTextNode(val.slice(cursor, h.start)));
        }
        const span = doc.createElement("span");
        span.className = "glossary-term";
        span.setAttribute("data-glossary-id", h.id);
        span.setAttribute("tabindex", "0");
        span.textContent = h.text;
        frag.appendChild(span);
        cursor = h.end;
        count += 1;
      }
      if (cursor < val.length) {
        frag.appendChild(doc.createTextNode(val.slice(cursor)));
      }
      if (tn.parentNode) {
        tn.parentNode.replaceChild(frag, tn);
      }
    }

    if (blockEl.setAttribute) {
      blockEl.setAttribute("data-glossary-scanned", "1");
    }
    return count;
  }

  function scanContainers(rootEl, blockSelector) {
    if (!rootEl) return 0;
    if (!isEnabled()) return 0;
    let total = 0;
    const blocks = blockSelector && rootEl.querySelectorAll ? rootEl.querySelectorAll(blockSelector) : [];
    if (blocks && blocks.length > 0) {
      blocks.forEach((b) => {
        total += scanBlock(b, MAX_TERMS_PER_BLOCK);
      });
    } else {
      total += scanBlock(rootEl, MAX_TERMS_PER_BLOCK);
    }
    return total;
  }

  function unwrapAll(rootEl) {
    const target = rootEl || (typeof document !== "undefined" ? document : null);
    if (!target || !target.querySelectorAll) return;
    target.querySelectorAll("span.glossary-term").forEach((span) => {
      const parent = span.parentNode;
      if (!parent) return;
      parent.replaceChild((span.ownerDocument || document).createTextNode(span.textContent || ""), span);
      if (parent.normalize) parent.normalize();
    });
    target.querySelectorAll("[data-glossary-scanned]").forEach((el) => {
      el.removeAttribute("data-glossary-scanned");
    });
  }

  // ==================== Tooltip UI ====================
  let tooltipEl = null;
  let activeSpan = null;

  function ensureTooltipEl() {
    if (typeof document === "undefined") return null;
    if (tooltipEl) return tooltipEl;
    tooltipEl = document.createElement("div");
    tooltipEl.id = "glossary-tooltip";
    tooltipEl.className = "glossary-tooltip hidden";
    tooltipEl.setAttribute("role", "tooltip");
    document.body.appendChild(tooltipEl);
    return tooltipEl;
  }

  function hideTooltip() {
    if (tooltipEl) tooltipEl.classList.add("hidden");
    activeSpan = null;
  }

  function showTooltipForSpan(span) {
    if (!span || !isEnabled()) return;
    const id = span.getAttribute("data-glossary-id");
    const entry = TERM_BY_ID[id];
    if (!entry) return;
    const tip = ensureTooltipEl();
    if (!tip) return;
    activeSpan = span;
    tip.innerHTML =
      `<div class="gt-head"><strong>${esc(entry.term)}</strong><span class="gt-en">${esc(entry.en)}</span><span class="gt-cat">${esc(entry.category)}</span></div>` +
      `<div class="gt-plain">${esc(entry.plain)}</div>` +
      `<div class="gt-trade"><strong>交易意義：</strong>${esc(entry.trading_meaning)}</div>`;
    tip.classList.remove("hidden");

    const rect = span.getBoundingClientRect();
    const tipRect = tip.getBoundingClientRect();
    const vw = window.innerWidth || 1024;
    const vh = window.innerHeight || 768;
    let left = rect.left;
    if (left + tipRect.width > vw - 12) {
      left = Math.max(12, vw - tipRect.width - 12);
    }
    let top = rect.bottom + 8;
    if (top + tipRect.height > vh - 12 && rect.top - tipRect.height - 8 > 8) {
      top = rect.top - tipRect.height - 8;
    }
    tip.style.left = `${Math.round(left)}px`;
    tip.style.top = `${Math.round(top)}px`;
  }

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function bindTooltipEvents() {
    if (typeof document === "undefined") return;
    document.addEventListener("mouseenter", (e) => {
      const span = e.target && e.target.closest ? e.target.closest(".glossary-term") : null;
      if (span) showTooltipForSpan(span);
    }, true);
    document.addEventListener("mouseleave", (e) => {
      const span = e.target && e.target.closest ? e.target.closest(".glossary-term") : null;
      if (span && span === activeSpan) hideTooltip();
    }, true);
    document.addEventListener("focusin", (e) => {
      const span = e.target && e.target.closest ? e.target.closest(".glossary-term") : null;
      if (span) showTooltipForSpan(span);
    });
    document.addEventListener("focusout", (e) => {
      const span = e.target && e.target.closest ? e.target.closest(".glossary-term") : null;
      if (span && span === activeSpan) hideTooltip();
    });
    document.addEventListener("click", (e) => {
      const span = e.target && e.target.closest ? e.target.closest(".glossary-term") : null;
      if (span) {
        const isSmallOrTouch = (window.innerWidth && window.innerWidth < 768) || ("ontouchstart" in window);
        if (isSmallOrTouch) {
          e.preventDefault();
          e.stopPropagation();
          if (activeSpan === span && tooltipEl && !tooltipEl.classList.contains("hidden")) {
            hideTooltip();
          } else {
            showTooltipForSpan(span);
          }
          return;
        }
      }
      if (tooltipEl && !tooltipEl.classList.contains("hidden") && (!e.target || !e.target.closest || !e.target.closest("#glossary-tooltip"))) {
        if (!span) hideTooltip();
      }
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" || e.key === "Esc") hideTooltip();
    });
  }

  // ==================== Dictionary Modal (Macro Footer) ====================
  let modalCategory = "all";
  let modalQuery = "";

  function renderGlossaryModalBody(bodyEl) {
    if (!bodyEl) return;
    const cats = ["all", "央行", "通膨", "就業", "景氣", "市場", "加密"];
    const q = modalQuery.trim().toLowerCase();
    const filtered = GLOSSARY_TERMS.filter((t) => {
      if (modalCategory !== "all" && t.category !== modalCategory) return false;
      if (!q) return true;
      return (
        t.term.toLowerCase().includes(q) ||
        t.en.toLowerCase().includes(q) ||
        t.plain.toLowerCase().includes(q) ||
        t.trading_meaning.toLowerCase().includes(q)
      );
    });

    const catBtns = cats
      .map((c) => {
        const label = c === "all" ? `全部（${GLOSSARY_TERMS.length}）` : c;
        return `<button type="button" class="macro-seg-btn${modalCategory === c ? " active" : ""}" data-gcat="${esc(c)}">${esc(label)}</button>`;
      })
      .join("");

    const cards = filtered
      .map(
        (t) => `
        <article class="glossary-dict-card">
          <div class="glossary-dict-head">
            <div>
              <strong>${esc(t.term)}</strong>
              <span class="muted glossary-dict-en">${esc(t.en)}</span>
            </div>
            <span class="glossary-dict-cat">${esc(t.category)}</span>
          </div>
          <p class="glossary-dict-plain">${esc(t.plain)}</p>
          <p class="glossary-dict-trade"><strong>對交易的意義：</strong>${esc(t.trading_meaning)}</p>
        </article>
      `
      )
      .join("");

    bodyEl.innerHTML = `
      <div class="glossary-modal-controls">
        <input id="glossary-search-input" class="glossary-search-input" type="search" placeholder="搜尋名詞、英文縮寫或關鍵字（如 FedWatch、PCE、OI）..." value="${esc(modalQuery)}" />
        <div class="macro-seg" id="glossary-cat-tabs" role="group" aria-label="辭典分類">${catBtns}</div>
      </div>
      <div class="muted" style="font-size:0.78rem;margin:8px 0;">顯示 ${filtered.length} / ${GLOSSARY_TERMS.length} 則名詞</div>
      <div class="glossary-dict-grid">${cards || '<div class="empty-state">找不到符合條件的名詞。</div>'}</div>
    `;

    const inp = bodyEl.querySelector("#glossary-search-input");
    if (inp) {
      inp.addEventListener("input", (e) => {
        modalQuery = e.target.value || "";
        const selStart = e.target.selectionStart;
        renderGlossaryModalBody(bodyEl);
        const newInp = bodyEl.querySelector("#glossary-search-input");
        if (newInp && newInp.focus) {
          newInp.focus();
          if (selStart != null && newInp.setSelectionRange) newInp.setSelectionRange(selStart, selStart);
        }
      });
    }
    const catTabs = bodyEl.querySelector("#glossary-cat-tabs");
    if (catTabs) {
      catTabs.addEventListener("click", (e) => {
        const btn = e.target && e.target.closest ? e.target.closest("[data-gcat]") : null;
        if (!btn) return;
        modalCategory = btn.getAttribute("data-gcat") || "all";
        renderGlossaryModalBody(bodyEl);
      });
    }
  }

  function updateToggleBtnText(btn) {
    if (!btn) return;
    const on = isEnabled();
    btn.textContent = `名詞提示：${on ? "開" : "關"}`;
    btn.setAttribute("aria-pressed", on ? "true" : "false");
  }

  function initToggle(btnId, onRescan) {
    if (typeof document === "undefined") return;
    const btn = document.getElementById(btnId);
    if (!btn) return;
    updateToggleBtnText(btn);
    btn.addEventListener("click", () => {
      const next = !isEnabled();
      setEnabled(next);
      updateToggleBtnText(btn);
      hideTooltip();
      if (!next) {
        unwrapAll(document);
      } else if (typeof onRescan === "function") {
        onRescan();
      }
    });
  }

  if (typeof document !== "undefined") {
    bindTooltipEvents();
  }

  global.SIGNAL_DESK_GLOSSARY = {
    TERMS: GLOSSARY_TERMS,
    TERM_BY_ID,
    MAX_TERMS_PER_BLOCK,
    isEnabled,
    setEnabled,
    findTermMatchesInText,
    scanBlock,
    scanContainers,
    unwrapAll,
    hideTooltip,
    renderGlossaryModalBody,
    initToggle,
    updateToggleBtnText,
  };
})(typeof window !== "undefined" ? window : globalThis);
