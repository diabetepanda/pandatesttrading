(function (global) {
  "use strict";

  // 前端設定檔：即時價格僅供顯示，不影響 GitHub Actions 報單與模擬引擎判定
  global.SIGNAL_DESK_CONFIG = {
    DATA_BASE: "https://raw.githubusercontent.com/diabetepanda/pandatesttrading/data",
    DATA_REFRESH_MS: 60000,   // 讀取 GitHub data 分支頻率：60 秒
    LIVE_REFRESH_MS: 8000,    // 瀏覽器即時價格更新頻率：8 秒 (5~10 秒)
    STALE_MINUTES: 45,
    REQUEST_TIMEOUT_MS: 5000,

    // 支援瀏覽器 CORS 且免 API Key 的即時價格對照（共 33 檔加密貨幣）
    LIVE_CRYPTO_SYMBOLS: {
      BTCUSDT: { okx: "BTC-USDT", binance: "BTCUSDT", kraken: "XBTUSDT" },
      ETHUSDT: { okx: "ETH-USDT", binance: "ETHUSDT", kraken: "ETHUSDT" },
      SOLUSDT: { okx: "SOL-USDT", binance: "SOLUSDT", kraken: "SOLUSD" },
      BNBUSDT: { okx: "BNB-USDT", binance: "BNBUSDT", kraken: null },
      XRPUSDT: { okx: "XRP-USDT", binance: "XRPUSDT", kraken: "XRPUSD" },
      DOGEUSDT: { okx: "DOGE-USDT", binance: "DOGEUSDT", kraken: null },
      ADAUSDT: { okx: "ADA-USDT", binance: "ADAUSDT", kraken: "ADAUSDT" },
      AVAXUSDT: { okx: "AVAX-USDT", binance: "AVAXUSDT", kraken: "AVAXUSDT" },
      LINKUSDT: { okx: "LINK-USDT", binance: "LINKUSDT", kraken: "LINKUSDT" },
      DOTUSDT: { okx: "DOT-USDT", binance: "DOTUSDT", kraken: "DOTUSDT" },
      LTCUSDT: { okx: "LTC-USDT", binance: "LTCUSDT", kraken: "LTCUSDT" },
      TRXUSDT: { okx: "TRX-USDT", binance: "TRXUSDT", kraken: "TRXUSD" },
      BCHUSDT: { okx: "BCH-USDT", binance: "BCHUSDT", kraken: "BCHUSDT" },
      NEARUSDT: { okx: "NEAR-USDT", binance: "NEARUSDT", kraken: "NEARUSD" },
      SUIUSDT: { okx: "SUI-USDT", binance: "SUIUSDT", kraken: "SUIUSD" },
      TONUSDT: { okx: null, binance: null, kraken: "TONUSDT" },
      APTUSDT: { okx: "APT-USDT", binance: "APTUSDT", kraken: "APTUSD" },
      UNIUSDT: { okx: "UNI-USDT", binance: "UNIUSDT", kraken: "UNIUSD" },
      ATOMUSDT: { okx: "ATOM-USDT", binance: "ATOMUSDT", kraken: "ATOMUSDT" },
      ETCUSDT: { okx: "ETC-USDT", binance: "ETCUSDT", kraken: "XETCZUSD" },
      FILUSDT: { okx: "FIL-USDT", binance: "FILUSDT", kraken: "FILUSD" },
      ARBUSDT: { okx: "ARB-USDT", binance: "ARBUSDT", kraken: "ARBUSD" },
      OPUSDT: { okx: "OP-USDT", binance: "OPUSDT", kraken: "OPUSD" },
      INJUSDT: { okx: "INJ-USDT", binance: "INJUSDT", kraken: "INJUSD" },
      AAVEUSDT: { okx: "AAVE-USDT", binance: "AAVEUSDT", kraken: "AAVEUSD" },
      XLMUSDT: { okx: "XLM-USDT", binance: "XLMUSDT", kraken: "XXLMZUSD" },
      HBARUSDT: { okx: "HBAR-USDT", binance: "HBARUSDT", kraken: "HBARUSD" },
      ICPUSDT: { okx: "ICP-USDT", binance: "ICPUSDT", kraken: "ICPUSD" },
      SEIUSDT: { okx: "SEI-USDT", binance: "SEIUSDT", kraken: "SEIUSD" },
      TIAUSDT: { okx: "TIA-USDT", binance: "TIAUSDT", kraken: "TIAUSD" },
      PEPEUSDT: { okx: "PEPE-USDT", binance: "PEPEUSDT", kraken: "PEPEUSD" },
      WLDUSDT: { okx: "WLD-USDT", binance: "WLDUSDT", kraken: "WLDUSD" },
      RENDERUSDT: { okx: "RENDER-USDT", binance: "RENDERUSDT", kraken: "RENDERUSD" },
    },

    // 金屬商品主來源 (api.gold-api.com 支援 CORS)
    LIVE_METAL_SYMBOLS: {
      XAUUSD: "XAU",
      XAGUSD: "XAG",
      COPPER: "HG",
    },

    // TradingView 公開報價端點代碼（外匯 10 檔與能源 2 檔主來源，金屬備援與漲跌幅來源）
    LIVE_TRADINGVIEW_SYMBOLS: {
      EURUSD: "FX:EURUSD",
      GBPUSD: "FX:GBPUSD",
      USDJPY: "FX:USDJPY",
      AUDUSD: "FX:AUDUSD",
      USDCAD: "FX:USDCAD",
      USDCHF: "FX:USDCHF",
      NZDUSD: "FX:NZDUSD",
      EURJPY: "FX:EURJPY",
      EURGBP: "FX:EURGBP",
      GBPJPY: "FX:GBPJPY",
      XAUUSD: "COMEX:GC1!",
      XAGUSD: "COMEX:SI1!",
      COPPER: "COMEX:HG1!",
      WTI: "NYMEX:CL1!",
      NATGAS: "NYMEX:NG1!",
    },
  };
})(window);
