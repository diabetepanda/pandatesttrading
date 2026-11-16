(function (global) {
  "use strict";

  // 前端設定檔：即時價格僅供顯示，不影響 GitHub Actions 報單與模擬引擎判定
  global.SIGNAL_DESK_CONFIG = {
    DATA_BASE: "https://raw.githubusercontent.com/diabetepanda/pandatesttrading/data",
    DATA_REFRESH_MS: 60000,   // 讀取 GitHub data 分支頻率：60 秒
    LIVE_REFRESH_MS: 8000,    // 瀏覽器即時價格更新頻率：8 秒 (5~10 秒)
    STALE_MINUTES: 45,
    REQUEST_TIMEOUT_MS: 5000,

    // 支援瀏覽器 CORS 且免 API Key 的即時價格對照
    LIVE_CRYPTO_SYMBOLS: {
      BTCUSDT: { okx: "BTC-USDT", binance: "BTCUSDT", kraken: "XBTUSDT" },
      ETHUSDT: { okx: "ETH-USDT", binance: "ETHUSDT", kraken: "ETHUSDT" },
      SOLUSDT: { okx: "SOL-USDT", binance: "SOLUSDT", kraken: "SOLUSD" },
      BNBUSDT: { okx: "BNB-USDT", binance: "BNBUSDT", kraken: null },
      XRPUSDT: { okx: "XRP-USDT", binance: "XRPUSDT", kraken: "XRPUSD" },
    },

    // 金屬商品即時價格 (api.gold-api.com 支援 CORS)
    LIVE_METAL_SYMBOLS: {
      XAUUSD: "XAU",
      XAGUSD: "XAG",
      COPPER: "HG",
    },
  };
})(window);
