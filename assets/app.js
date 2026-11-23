(function () {
  "use strict";

  const CFG = window.SIGNAL_DESK_CONFIG || {};
  const DATA_BASE = CFG.DATA_BASE || "https://raw.githubusercontent.com/diabetepanda/pandatesttrading/data";
  const STALE_MINUTES = CFG.STALE_MINUTES || 45;
  const DATA_REFRESH_MS = CFG.DATA_REFRESH_MS || 60000;
  const LIVE_REFRESH_MS = CFG.LIVE_REFRESH_MS || 8000;
  const REQUEST_TIMEOUT_MS = CFG.REQUEST_TIMEOUT_MS || 5000;
  const LIVE_CRYPTO_SYMBOLS = CFG.LIVE_CRYPTO_SYMBOLS || {};
  const LIVE_METAL_SYMBOLS = CFG.LIVE_METAL_SYMBOLS || {};
  const LIVE_TRADINGVIEW_SYMBOLS = CFG.LIVE_TRADINGVIEW_SYMBOLS || {};

  let currentMarketFilter = "all";
  let currentWallTab = "active";
  let currentWatchScope = "intraday";
  let currentWatchCategory = "all";
  let showAllWatch = false;
  let cachedInstruments = [];
  let cachedSignals = [];
  let cachedTrades = [];
  let cachedSignalById = {};
  let cachedWatch = null;
  let cachedWatchDaily = null;
  let cachedBacktest = null;
  let cachedPerf = null;
  let cachedRisk = null;
  let cachedEquity = null;
  let cachedEvents = null;
  let cachedTa = null;
  let cachedFlowTw = null;
  let pendingResearchArticleId = null;
  const expandedResearchIds = new Set(["short_term"]);
  let lastRenderedResearchStamp = null;
  let cachedNewsLatest = null;
  let cachedNewsIndex = null;
  let cachedNewsReactions = null;
  const newsMonthCache = {};
  let newsSelectedIssue = "";
  let newsSelectedMarket = "all";
  let newsSelectedSentiment = "all";
  let newsLoadingMonth = false;
  let pendingNewsLatest = null;
  let pendingNewsIndex = null;
  let pendingNewsReactions = null;
  let newsPrevVisitMs = 0;
  let newsVisitRecorded = false;
  const expandedNewsReasonIds = new Set();
  const NEWS_LAST_VISIT_KEY = "signal_desk_news_last_visit";
  const NEWS_POLL_MS = 5 * 60 * 1000;
  let expandedTaSymbol = null;
  let livePrices = {};
  let activeChart = null;
  let activeEquityChart = null;
  let openNowCopyById = {};
  let openNowCopiedAt = {};
  let openNowAllCopyText = "";
  const expandedWallSignalIds = new Set();

  const SITE_URL = "https://diabetepanda.github.io/pandatesttrading/";
  const OPEN_NOW_STRATEGIES = new Set(["S2", "S3", "L1", "L2", "L3", "L4"]);

  // 卡片建議的槓桿倍數，可調整
  const SUGGESTED_LEVERAGE = 10;

  const STRATEGY_NAMES = {
    S1: "S1 開盤區間突破（已停用）",
    S2: "S2 4 小時突破（短線）",
    S3: "S3 日線 RSI 回檔（短線）",
    L1: "L1 波動度調整趨勢跟隨（長線）",
    L2: "L2 精簡標的趨勢跟隨（長線）",
    L3: "L3 雙動能（長線）",
    L4: "L4 主流週線趨勢（長線）",
  };

  const S1_RETIRED_NOTE =
    "經討論後停用：這套做法實際跑下來會虧錢，5 年回測虧損 97%，已由 L4 主流週線趨勢取代。歷史紀錄僅供參考。";

  function formatTaipeiTime(isoOrDate) {
    if (!isoOrDate) return "--";
    const dt = isoOrDate instanceof Date ? isoOrDate : new Date(isoOrDate);
    if (Number.isNaN(dt.getTime())) return String(isoOrDate);
    const parts = new Intl.DateTimeFormat("en-US", {
      timeZone: "Asia/Taipei",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    }).formatToParts(dt);
    const map = {};
    for (const p of parts) map[p.type] = p.value;
    const hh = map.hour === "24" ? "00" : (map.hour || "00");
    return `${map.month || "01"}/${map.day || "01"} ${hh}:${map.minute || "00"}`;
  }

  function getPricePrecision(val) {
    const abs = Math.abs(Number(val));
    if (!Number.isFinite(abs) || abs === 0) return 2;
    if (abs >= 1000) return 2;
    if (abs >= 10) return 3;
    if (abs >= 1) return 4;
    const leadingZeros = Math.max(0, -Math.floor(Math.log10(abs)) - 1);
    return Math.min(12, Math.max(4, leadingZeros + 4));
  }

  function formatNumber(val) {
    if (val === null || val === undefined || Number.isNaN(Number(val))) return null;
    const num = Number(val);
    if (num === 0) return "0";
    return num.toLocaleString("en-US", { maximumFractionDigits: getPricePrecision(num) });
  }

  function formatExactPrice(val) {
    if (val === null || val === undefined || val === "" || Number.isNaN(Number(val))) return null;
    const num = Number(val);
    if (!Number.isFinite(num)) return null;
    if (num === 0) return "0";
    const raw = String(num);
    if (!/[eE]/.test(raw)) return raw;
    return num.toFixed(12).replace(/\.?0+$/, "");
  }

  function getCardPriceDecimals(val) {
    const abs = Math.abs(Number(val));
    if (!Number.isFinite(abs) || abs === 0) return 2;
    if (abs >= 1000) return 2;
    if (abs >= 100) return 3;
    if (abs >= 1) return 5;
    const leadingZeros = Math.max(0, -Math.floor(Math.log10(abs)) - 1);
    return Math.max(2, leadingZeros + 5);
  }

  function formatCardPrice(val, forcedDecimals) {
    if (val === null || val === undefined || val === "" || Number.isNaN(Number(val))) return "—";
    const num = Number(val);
    if (!Number.isFinite(num)) return "—";
    const decimals = Number.isInteger(forcedDecimals) && forcedDecimals >= 0
      ? forcedDecimals
      : getCardPriceDecimals(num);
    return num.toFixed(decimals);
  }

  function formatCardPricePair(entryVal, slVal) {
    const entryDec = getCardPriceDecimals(entryVal);
    const slDec = getCardPriceDecimals(slVal);
    let entryStr = formatCardPrice(entryVal, entryDec);
    let slStr = formatCardPrice(slVal, slDec);
    if (entryStr !== "—" && slStr !== "—" && entryStr === slStr && Number(entryVal) !== Number(slVal)) {
      let dec = Math.max(entryDec, slDec);
      const limit = Math.max(8, dec);
      while (entryStr === slStr && dec < limit) {
        dec += 1;
        entryStr = formatCardPrice(entryVal, dec);
        slStr = formatCardPrice(slVal, dec);
      }
    }
    return { entryStr, slStr };
  }

  function formatPercentDisplay(val) {
    const num = Number(val);
    if (!Number.isFinite(num) || num <= 0) return "0%";
    if (num < 0.1) return "<0.1%";
    const rounded = Math.round(num * 10) / 10;
    if (rounded % 1 === 0) {
      return `${Math.round(rounded)}%`;
    }
    return `${rounded.toFixed(1)}%`;
  }

  function convertReasonUtcToTaipei(text) {
    if (!text) return "";
    let out = String(text);
    // ISO UTC timestamps like 2026-11-16T08:00:00Z
    out = out.replace(/(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?Z)/g, (m) => `台北 ${formatTaipeiTime(m)}`);
    // "UTC 08:00" or "08:00 UTC"
    out = out.replace(/\bUTC\s+(\d{1,2}):(\d{2})\b/gi, (_, h, m) => {
      const th = (Number(h) + 8) % 24;
      return `台北 ${String(th).padStart(2, "0")}:${m}`;
    });
    out = out.replace(/\b(\d{1,2}):(\d{2})\s*UTC\b/gi, (_, h, m) => {
      const th = (Number(h) + 8) % 24;
      return `台北 ${String(th).padStart(2, "0")}:${m}`;
    });
    return out;
  }

  function formatUsd(val) {
    if (val === undefined || val === null || Number.isNaN(Number(val))) return "$100,000.00";
    return "$" + Number(val).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  function formatChange(pct) {
    if (pct === null || pct === undefined || Number.isNaN(Number(pct))) {
      return '<span class="muted">--</span>';
    }
    const val = Number(pct);
    const sign = val > 0 ? "+" : "";
    const cls = val > 0 ? "chg-up" : val < 0 ? "chg-down" : "muted";
    return `<span class="${cls}">${sign}${val.toFixed(2)}%</span>`;
  }

  function statusPill(status) {
    if (status === "ok") return '<span class="pill pill-ok">正常</span>';
    if (status === "stale") return '<span class="pill pill-warn">延遲/休市</span>';
    if (status === "unstable") return '<span class="pill pill-warn">資料不穩</span>';
    if (status === "skipped") return '<span class="muted">僅日線</span>';
    return '<span class="pill pill-fail">資料來源失敗</span>';
  }

  function lifecycleLabel(st) {
    const map = {
      active: "進行中",
      closed: "已結束",
      cancelled: "已取消",
    };
    return map[st] || st;
  }

  function exitReasonLabel(reason) {
    const map = {
      stop_loss: "止損",
      trailing_stop: "移動停利",
      time_stop: "時間出場",
      session_stop: "強制平倉",
      rebalance_exit: "調出",
      take_profit: "止盈",
      logic_invalidated: "移動停利",
      risk_forced_close: "強制平倉",
      instrument_disabled: "標的停用平倉",
      trend_exit: "跌破均線出場",
      strategy_retired: "策略停用平倉",
      expired: "已取消",
      pre_entry_invalidated: "已取消",
      risk_cancelled: "已取消",
    };
    return map[reason] || reason || "--";
  }

  function getUiMarketKey(market, symbol, instrumentObj) {
    const m = String(market || "");
    if (m === "tw_stock" || m === "us_stock") return m;
    if (m !== "stock") return m;
    const inst = instrumentObj || cachedInstruments.find((i) => i && i.symbol === symbol) || null;
    const th = String(inst?.trading_hours || "");
    const grp = String(inst?.correlation_group || "");
    if (th === "TW_STOCK" || grp.startsWith("tw_") || /^\d{4}$/.test(String(symbol || ""))) {
      return "tw_stock";
    }
    return "us_stock";
  }

  function formatInstrumentTitle(symbol, instrumentObj) {
    const sym = String(symbol || "");
    const inst = instrumentObj || cachedInstruments.find((i) => i && i.symbol === sym) || null;
    if (inst && inst.display_name && (inst.market === "stock" || inst.market === "index")) {
      return inst.display_name;
    }
    return sym;
  }

  function marketLabel(market, symbol, instrumentObj) {
    const key = getUiMarketKey(market, symbol, instrumentObj);
    const map = { crypto: "加密貨幣", tw_stock: "台股", us_stock: "美股／個股", stock: "美股／個股", commodity: "商品", index: "指數" };
    return map[key] || (key === "forex" ? "歷史標的" : key);
  }

  async function fetchWithTimeout(url, asText = false) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    try {
      const resp = await fetch(url, { cache: "no-store", signal: controller.signal });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      return asText ? await resp.text() : await resp.json();
    } finally {
      clearTimeout(timer);
    }
  }

  async function fetchDataJson(filename) {
    return fetchWithTimeout(`${DATA_BASE}/${filename}?t=${Date.now()}`, false);
  }

  async function fetchDataText(filename) {
    return fetchWithTimeout(`${DATA_BASE}/${filename}?t=${Date.now()}`, true);
  }

  async function fetchTaData() {
    try {
      const data = await fetchDataJson("ta.json");
      if (data && data.symbols) return data;
    } catch (_err) {
      // fallback to local data/ta.json when data branch does not yet have ta.json
    }
    return fetchWithTimeout(`data/ta.json?t=${Date.now()}`, false);
  }

  async function fetchFlowTwData() {
    if (window.SIGNAL_DESK_FLOW_TW && typeof window.SIGNAL_DESK_FLOW_TW.fetchFlowTwJson === "function") {
      return window.SIGNAL_DESK_FLOW_TW.fetchFlowTwJson(DATA_BASE, REQUEST_TIMEOUT_MS);
    }
    try {
      const data = await fetchDataJson("flow_tw.json");
      if (data && (data.a1 || data.a2)) return data;
    } catch (_err) {
      // fallback to local data/flow_tw.json
    }
    return fetchWithTimeout(`data/flow_tw.json?t=${Date.now()}`, false);
  }

  async function fetchNewsLatest() {
    try {
      const data = await fetchDataJson("news_latest.json");
      if (data && data.latest_issue) return data;
    } catch (_err) {
      // fallback to local data/news_latest.json
    }
    return fetchWithTimeout(`data/news_latest.json?t=${Date.now()}`, false);
  }

  async function fetchNewsIndex() {
    try {
      const data = await fetchDataJson("news_index.json");
      if (data && Array.isArray(data.issues)) return data;
    } catch (_err) {
      // fallback to local data/news_index.json
    }
    return fetchWithTimeout(`data/news_index.json?t=${Date.now()}`, false);
  }

  async function fetchNewsReactions() {
    try {
      const data = await fetchDataJson("news_reactions.json");
      if (data && typeof data === "object") return data;
    } catch (_err) {
      // fallback to local data/news_reactions.json
    }
    try {
      return await fetchWithTimeout(`data/news_reactions.json?t=${Date.now()}`, false);
    } catch (_err2) {
      return null;
    }
  }

  async function fetchNewsMonthJsonl(month) {
    if (!month) return [];
    if (Array.isArray(newsMonthCache[month])) return newsMonthCache[month];
    let text = "";
    try {
      text = await fetchDataText(`news/${encodeURIComponent(month)}.jsonl`);
    } catch (_err) {
      text = await fetchWithTimeout(`data/news/${encodeURIComponent(month)}.jsonl?t=${Date.now()}`, true);
    }
    const rows = String(text || "")
      .split("\n")
      .map((l) => l.trim())
      .filter(Boolean)
      .map((l) => { try { return JSON.parse(l); } catch (_) { return null; } })
      .filter((x) => x && x.id);
    newsMonthCache[month] = rows;
    return rows;
  }

  function isLiveCapable(symbol) {
    return Boolean(
      LIVE_CRYPTO_SYMBOLS[symbol] || LIVE_METAL_SYMBOLS[symbol] || LIVE_TRADINGVIEW_SYMBOLS[symbol]
    );
  }

  async function fetchLiveCrypto() {
    const results = {};
    const pending = new Set(Object.keys(LIVE_CRYPTO_SYMBOLS));

    try {
      const okxData = await fetchWithTimeout("https://www.okx.com/api/v5/market/tickers?instType=SPOT");
      if (okxData && okxData.code === "0" && Array.isArray(okxData.data)) {
        const byInst = {};
        for (const row of okxData.data) byInst[row.instId] = row;
        for (const sym of Array.from(pending)) {
          const okxCode = LIVE_CRYPTO_SYMBOLS[sym].okx;
          const row = byInst[okxCode];
          if (row && row.last) {
            const last = Number(row.last);
            const open24h = Number(row.open24h);
            const chg = open24h > 0 ? ((last - open24h) / open24h) * 100 : null;
            results[sym] = { price: last, changePct: chg, source: "OKX" };
            pending.delete(sym);
          }
        }
      }
    } catch (_) {}

    if (pending.size > 0) {
      try {
        const binSymbols = Array.from(pending).map((s) => LIVE_CRYPTO_SYMBOLS[s].binance).filter(Boolean);
        if (binSymbols.length > 0) {
          const qs = encodeURIComponent(JSON.stringify(binSymbols));
          const binData = await fetchWithTimeout(`https://data-api.binance.vision/api/v3/ticker/24hr?symbols=${qs}`);
          if (Array.isArray(binData)) {
            const bySym = {};
            for (const row of binData) bySym[row.symbol] = row;
            for (const sym of Array.from(pending)) {
              const binCode = LIVE_CRYPTO_SYMBOLS[sym].binance;
              const row = bySym[binCode];
              if (row && row.lastPrice) {
                results[sym] = {
                  price: Number(row.lastPrice),
                  changePct: row.priceChangePercent !== undefined ? Number(row.priceChangePercent) : null,
                  source: "Binance",
                };
                pending.delete(sym);
              }
            }
          }
        }
      } catch (_) {}
    }

    if (pending.size > 0) {
      try {
        const krakenPairs = Array.from(pending).map((s) => LIVE_CRYPTO_SYMBOLS[s].kraken).filter(Boolean);
        if (krakenPairs.length > 0) {
          const krData = await fetchWithTimeout(
            `https://api.kraken.com/0/public/Ticker?pair=${encodeURIComponent(krakenPairs.join(","))}`
          );
          if (krData && (!krData.error || krData.error.length === 0) && krData.result) {
            for (const sym of Array.from(pending)) {
              const kCode = LIVE_CRYPTO_SYMBOLS[sym].kraken;
              if (!kCode) continue;
              const entry =
                krData.result[kCode] ||
                Object.entries(krData.result).find(([k]) => k.includes(kCode.replace("USDT", "")))?.[1];
              if (entry && entry.c && entry.c[0]) {
                const last = Number(entry.c[0]);
                const open = entry.o ? Number(entry.o) : null;
                const chg = open && open > 0 ? ((last - open) / open) * 100 : null;
                results[sym] = { price: last, changePct: chg, source: "Kraken" };
                pending.delete(sym);
              }
            }
          }
        }
      } catch (_) {}
    }

    for (const sym of pending) results[sym] = { failed: true };
    return results;
  }

  async function fetchTradingViewSymbol(tvCode) {
    const url = `https://scanner.tradingview.com/symbol?symbol=${encodeURIComponent(tvCode)}&fields=close,change&no_404=true`;
    const data = await fetchWithTimeout(url);
    if (data && data.close !== undefined && data.close !== null && !Number.isNaN(Number(data.close))) {
      return {
        price: Number(data.close),
        changePct: data.change !== undefined && data.change !== null ? Number(data.change) : null,
        source: "TradingView",
      };
    }
    return null;
  }

  async function fetchTradingViewBatch(tvCodes) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    try {
      const resp = await fetch("https://scanner.tradingview.com/global/scan", {
        method: "POST",
        cache: "no-store",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          symbols: { tickers: tvCodes },
          columns: ["close", "change"],
        }),
        signal: controller.signal,
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const payload = await resp.json();
      const map = {};
      for (const row of payload?.data || []) {
        const vals = row?.d || [];
        if (row?.s && vals[0] !== undefined && vals[0] !== null && !Number.isNaN(Number(vals[0]))) {
          map[row.s] = {
            price: Number(vals[0]),
            changePct: vals[1] !== undefined && vals[1] !== null ? Number(vals[1]) : null,
            source: "TradingView",
          };
        }
      }
      return map;
    } finally {
      clearTimeout(timer);
    }
  }

  async function fetchLiveTradFi() {
    const results = {};
    const tvSymbols = Object.keys(LIVE_TRADINGVIEW_SYMBOLS);
    let tvMap = {};
    try {
      tvMap = await fetchTradingViewBatch(Object.values(LIVE_TRADINGVIEW_SYMBOLS));
    } catch (_) {
      tvMap = {};
    }

    await Promise.all(
      tvSymbols.map(async (sym) => {
        const metalCode = LIVE_METAL_SYMBOLS[sym];
        const tvCode = LIVE_TRADINGVIEW_SYMBOLS[sym];
        let tvOk = tvMap[tvCode] || null;
        if (!tvOk) {
          try {
            tvOk = await fetchTradingViewSymbol(tvCode);
          } catch (_) {
            tvOk = null;
          }
        }
        if (metalCode) {
          try {
            const goldVal = await fetchWithTimeout(`https://api.gold-api.com/price/${metalCode}`);
            if (goldVal && goldVal.price !== undefined) {
              results[sym] = {
                price: Number(goldVal.price),
                changePct: tvOk ? tvOk.changePct : null,
                source: "gold-api",
              };
              return;
            }
          } catch (_) {}
          results[sym] = tvOk || { failed: true };
          return;
        }
        results[sym] = tvOk || { failed: true };
      })
    );
    return results;
  }

  function getDisplayPrice(symbol) {
    const live = livePrices[symbol];
    if (live && !live.failed && live.price !== undefined) {
      return { price: live.price, source: `即時 ${live.source}`, isLive: true, failed: false };
    }
    const inst = cachedInstruments.find((i) => i.symbol === symbol);
    const schedPrice = inst && inst.latest_price !== null && inst.latest_price !== undefined ? Number(inst.latest_price) : null;
    return {
      price: schedPrice,
      source: live && live.failed ? "即時價格來源失敗（退回排程價）" : "排程價",
      isLive: false,
      failed: Boolean(live && live.failed),
    };
  }

  function formatDist(livePrice, targetPrice, direction, isStop) {
    if (livePrice === null || targetPrice === null || targetPrice === undefined || Number.isNaN(Number(targetPrice))) {
      return "--";
    }
    const t = Number(targetPrice);
    const diff = t - livePrice;
    const pct = livePrice > 0 ? (diff / livePrice) * 100 : 0;
    const sign = diff > 0 ? "+" : "";
    return `${formatNumber(t)} (${sign}${pct.toFixed(2)}%)`;
  }

  function computeFloatingPnl(sig, livePrice) {
    if (livePrice === null || sig.lifecycle_state !== "active") {
      return null;
    }
    const avg = Number(sig.avg_entry_price || sig.entry_price || 0);
    const remQty = Number(sig.remaining_qty || 0);
    if (avg <= 0 || remQty <= 0) return null;
    const unrealized = sig.direction === "long" ? (livePrice - avg) * remQty : (avg - livePrice) * remQty;
    const totalEst = Number(sig.realized_pnl_usd || 0) + unrealized;
    const rPlan = Number(sig.r_plan_usd || sig.risk_usd || 1);
    const estR = rPlan > 0 ? totalEst / rPlan : 0;
    return { usd: totalEst, r: estR };
  }

  function fmtCompactUsd(val) {
    const num = Number(val || 0);
    if (Math.abs(num - Math.round(num)) < 0.01) {
      return Math.round(num).toLocaleString("en-US");
    }
    return num.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  function computeCardEstimates(s) {
    const strat = String(s.strategy || "S2");
    const subRow = Array.isArray(cachedRisk?.subaccounts)
      ? cachedRisk.subaccounts.find((x) => x.strategy_id === strat)
      : null;
    const defaultCap = strat.startsWith("S") ? 10000 : 20000;
    const subEq = Number(s.subaccount_capital_usd || subRow?.realized_equity_usd || defaultCap);
    const notional = Number(s.effective_notional_usd ?? s.notional_usd ?? s.planned_notional_usd ?? 0);
    const notionalPct = s.notional_pct_subaccount !== undefined && s.notional_pct_subaccount !== null
      ? Number(s.notional_pct_subaccount)
      : (subEq > 0 ? (notional / subEq) * 100 : 0);
    const slUsd = Number(s.estimated_stop_loss_usd ?? s.risk_usd ?? s.r_plan_usd ?? 0);
    const slPct = s.estimated_stop_loss_pct_pool !== undefined && s.estimated_stop_loss_pct_pool !== null
      ? Number(s.estimated_stop_loss_pct_pool)
      : (subEq > 0 ? (slUsd / subEq) * 100 : Number(s.risk_pct_pool || 1));
    return { subEq, notional, notionalPct, slUsd, slPct };
  }

  function getStrategyExitRuleText(s) {
    if (s && s.exit_rule) return convertReasonUtcToTaipei(s.exit_rule);
    if (s && s.strategy === "L4") {
      return "移動停損（近 60 日最高收盤 − K×ATR）或週收盤跌破 N 日均線";
    }
    return convertReasonUtcToTaipei(s?.invalidation_cond || "依策略規則出場");
  }

  function formatSignalCopyText(s) {
    const dirZh = s.direction === "long" ? "做多" : "做空";
    const stratZh = STRATEGY_NAMES[s.strategy] || `策略 ${s.strategy}`;
    const est = computeCardEstimates(s);
    const entryPx = Number(s.avg_entry_price || s.entry_price || s.entry_low || 0);
    const entryStr = formatNumber(entryPx) || String(entryPx);
    const curSl = s.current_stop_loss ?? s.stop_loss;
    const slStr = formatNumber(curSl) || String(curSl);
    const enteredTp = formatTaipeiTime(s.entered_at_utc || s.published_at_utc);
    const ruleTxt = getStrategyExitRuleText(s);
    return [
      `【Signal Desk 報單】${s.signal_id}（${stratZh}）`,
      `標的：${s.symbol}｜方向：${dirZh}`,
      `進場：市價 ${entryStr}（台北時間 ${enteredTp} 成交）`,
      `止損：${slStr}（打到預估虧損 -$${fmtCompactUsd(est.slUsd)}，佔 ${s.strategy} 策略資金 ${est.slPct.toFixed(1)}%）`,
      `出場規則：${ruleTxt}`,
      `倉位名目：$${fmtCompactUsd(est.notional)}（佔 ${s.strategy} 策略資金 ${est.notionalPct.toFixed(1)}%）`,
      "模擬驗證中，非投資建議",
    ].join("\n");
  }

  async function copyTextToClipboard(text) {
    if (!text) return false;
    if (typeof navigator !== "undefined" && navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
      try {
        await navigator.clipboard.writeText(text);
        return true;
      } catch (_) {
        // fallback below when Clipboard API is blocked or unavailable
      }
    }
    if (typeof document === "undefined" || !document.body || typeof document.createElement !== "function") {
      return false;
    }
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.top = "-9999px";
    ta.style.left = "-9999px";
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    let ok = false;
    try {
      ok = typeof document.execCommand === "function" && document.execCommand("copy");
    } catch (_) {
      ok = false;
    }
    document.body.removeChild(ta);
    return Boolean(ok);
  }

  function flashCopySuccess(btn, labelDefault) {
    if (!btn) return;
    btn.textContent = "已複製 ✓";
    clearTimeout(btn._copyResetTimer);
    btn._copyResetTimer = setTimeout(() => {
      btn.textContent = labelDefault;
    }, 2000);
  }

  function bindCopyButton(btn, textToCopy, labelDefault) {
    btn.addEventListener("click", async () => {
      const ok = await copyTextToClipboard(textToCopy);
      if (ok) {
        flashCopySuccess(btn, labelDefault);
      } else {
        btn.textContent = "請手動複製";
      }
    });
  }

  const SIMPLE_DISPLAY_NAMES = {
    XAUUSD: "Gold（黃金）",
    XAGUSD: "Silver（白銀）",
    COPPER: "Copper（銅）",
    WTI: "WTI（原油）",
    NATGAS: "Natural Gas（天然氣）",
    NAS100: "NASDAQ 100（那斯達克 100）",
    SP500: "S&P 500（標普 500）",
    JP225: "Nikkei 225（日經 225）",
    TWII: "TAIEX（台股加權指數）",
    HSI: "Hang Seng（恒生指數）",
  };

  function formatSimpleSymbolName(symbol, market, instrumentsList) {
    const sym = String(symbol || "");
    if (SIMPLE_DISPLAY_NAMES[sym]) return SIMPLE_DISPLAY_NAMES[sym];
    if (market === "crypto" || /USDT$/i.test(sym)) {
      return sym.replace(/USDT$/i, "");
    }
    const list = Array.isArray(instrumentsList) ? instrumentsList : cachedInstruments;
    const inst = list.find((i) => i.symbol === sym);
    if (inst && inst.display_name) {
      const m = String(inst.display_name).match(/^(.+?)\s*\(([^)]+)\)$/);
      if (m) return `${m[2]}（${m[1]}）`;
      return inst.display_name;
    }
    return sym;
  }

  function getStrategyPoolKind(strategyId) {
    if (strategyId === "L3") return "輪動";
    if (String(strategyId || "").startsWith("L")) return "長線";
    return "短線";
  }

  function isStrategyVerified(strategyId, signalObj, backtestObj) {
    const bt = backtestObj !== undefined ? backtestObj : cachedBacktest;
    const list = bt ? (Array.isArray(bt.strategies) ? bt.strategies : bt.results) : null;
    if (Array.isArray(list)) {
      const row = list.find((item) => item.strategy_id === strategyId);
      if (row) return Boolean(row.passed);
    }
    if (signalObj?.verification_status === "已驗證") return true;
    if (Array.isArray(signalObj?.reasons)) {
      if (signalObj.reasons.some((r) => String(r).trim() === "策略狀態：已驗證")) return true;
    }
    return false;
  }

  function extractSimpleReasonText(signalObj) {
    const reasons = Array.isArray(signalObj?.reasons) ? signalObj.reasons : [];
    const picked = reasons.find((r) => !String(r).trim().startsWith("策略狀態："));
    if (picked) return convertReasonUtcToTaipei(String(picked).trim());
    return "依策略訊號進場";
  }

  function computeSimpleExitPoint(tradeObj) {
    const sl = Number(tradeObj?.current_stop_loss ?? tradeObj?.initial_stop_loss ?? tradeObj?.stop_loss ?? 0);
    const rawTrail = tradeObj?.trailing_exit_price;
    const hasTrail = rawTrail !== null && rawTrail !== undefined && rawTrail !== "" && !Number.isNaN(Number(rawTrail));
    let exitPx = sl;
    if (hasTrail) {
      const trail = Number(rawTrail);
      exitPx = tradeObj?.direction === "short" ? Math.min(sl, trail) : Math.max(sl, trail);
    }
    const suffix = tradeObj?.strategy === "L3" ? "（另外每週檢查排名）" : "";
    return { price: exitPx, suffix };
  }

  function computeSimpleTradePnl(tradeObj, currentPrice) {
    if (currentPrice === null || currentPrice === undefined || currentPrice === "" || Number.isNaN(Number(currentPrice))) {
      return { available: false, roundedUsd: null, text: "—", cls: "muted" };
    }
    const cur = Number(currentPrice);
    const entry = Number(tradeObj?.avg_entry_price || tradeObj?.entry_price || 0);
    const qty = Number(tradeObj?.remaining_qty || 0);
    const realized = Number(tradeObj?.realized_pnl_usd || 0);
    const unrealized = tradeObj?.direction === "short" ? (entry - cur) * qty : (cur - entry) * qty;
    const total = unrealized + realized;
    let rounded = Math.round(total);
    if (Object.is(rounded, -0)) rounded = 0;
    if (rounded > 0) {
      return { available: true, roundedUsd: rounded, text: `+$${rounded.toLocaleString("en-US")}`, cls: "chg-up" };
    }
    if (rounded < 0) {
      return { available: true, roundedUsd: rounded, text: `-$${Math.abs(rounded).toLocaleString("en-US")}`, cls: "chg-down" };
    }
    return { available: true, roundedUsd: 0, text: "$0", cls: "muted" };
  }

  function buildSimpleSignalCardsData(tradesList, signalById, backtestObj, priceLookupFn, instrumentsList) {
    const active = (Array.isArray(tradesList) ? tradesList : []).filter((t) => t && t.state === "active");
    const cards = active.map((t) => {
      const sigObj = (signalById && signalById[t.signal_id]) || null;
      const isLong = t.direction === "long";
      const entryPx = Number(t.avg_entry_price || t.entry_price || 0);
      const slPx = Number(t.current_stop_loss ?? t.initial_stop_loss ?? t.stop_loss ?? 0);
      const notionalRaw = Number(t.planned_notional_usd || (Number(t.remaining_qty || 0) * entryPx) || 0);
      const marginUsd = Math.round(notionalRaw / 5);
      const notionalUsd = Math.round(notionalRaw);
      const verified = isStrategyVerified(t.strategy, sigObj, backtestObj);
      const exitInfo = computeSimpleExitPoint(t);
      const curPx = typeof priceLookupFn === "function" ? priceLookupFn(t.symbol) : getDisplayPrice(t.symbol).price;
      const pnlInfo = computeSimpleTradePnl(t, curPx);
      return {
        signal_id: t.signal_id,
        verified,
        notionalRaw,
        dot: isLong ? "🟢" : "🔴",
        displayName: formatSimpleSymbolName(t.symbol, t.market, instrumentsList),
        dirZh: isLong ? "做多" : "做空",
        reasonLine: `${extractSimpleReasonText(sigObj)}（${t.strategy} ${getStrategyPoolKind(t.strategy)}，${verified ? "已驗證" : "觀察中"}）`,
        entryStr: entryPx > 0 ? (formatNumber(entryPx) ?? "—") : "—",
        slStr: slPx > 0 ? (formatNumber(slPx) ?? "—") : "—",
        exitStr: exitInfo.price > 0 ? `${formatNumber(exitInfo.price) ?? "—"}${exitInfo.suffix}` : `—${exitInfo.suffix}`,
        posText: `本金 $${marginUsd.toLocaleString("en-US")} × 5 倍 = $${notionalUsd.toLocaleString("en-US")}`,
        pnlInfo,
      };
    });
    cards.sort((a, b) => (Number(b.verified) - Number(a.verified)) || (b.notionalRaw - a.notionalRaw));
    return cards;
  }

  function getSimpleStrategyShortName(strategyId, backtestObj) {
    const bt = backtestObj !== undefined ? backtestObj : cachedBacktest;
    const list = bt ? (Array.isArray(bt.strategies) ? bt.strategies : bt.results) : null;
    const row = Array.isArray(list) ? list.find((item) => item && item.strategy_id === strategyId) : null;
    if (row && row.name) return `${strategyId} ${row.name}`;
    const raw = STRATEGY_NAMES[strategyId] || strategyId;
    return raw.replace(/（(?:短線|長線|已停用)）$/g, "");
  }

  function computeOpenNowHistoryStats(strategyId, backtestObj) {
    const bt = backtestObj !== undefined ? backtestObj : cachedBacktest;
    const list = bt ? (Array.isArray(bt.strategies) ? bt.strategies : bt.results) : null;
    const row = Array.isArray(list) ? list.find((item) => item && item.strategy_id === strategyId) : null;
    const oos = row && row.out_of_sample;
    if (
      !oos ||
      Number(oos.trades || 0) <= 0 ||
      oos.win_rate_pct === null ||
      oos.win_rate_pct === undefined ||
      Number.isNaN(Number(oos.win_rate_pct)) ||
      oos.expectancy_r === null ||
      oos.expectancy_r === undefined ||
      Number.isNaN(Number(oos.expectancy_r))
    ) {
      return { available: false, copyStatsLine: "策略歷史數據暫無" };
    }
    const winRatePct = Number(oos.win_rate_pct);
    const winRateInt = Math.round(winRatePct);
    const wr = winRatePct / 100;
    const expR = Number(oos.expectancy_r);
    let avgWinR;
    if (oos.avg_win_r !== null && oos.avg_win_r !== undefined && Number.isFinite(Number(oos.avg_win_r))) {
      avgWinR = Number(oos.avg_win_r);
    } else if (wr > 0) {
      // backtest_results.json 的 out_of_sample 未提供贏單平均 R，
      // 故假設單筆輸單平均為 -1R，依 E[R] = 勝率 * avgWinR - (1 - 勝率) * 1
      // 反推公式：avgWinR = (期望R + (1 - 勝率)) ÷ 勝率
      avgWinR = (expR + (1 - wr)) / wr;
    } else {
      avgWinR = 0;
    }
    const avgWinAssetPct = avgWinR * 1.0;
    const avgWinRounded = Number(avgWinAssetPct.toFixed(1));
    const avgWinStr = `${avgWinRounded >= 0 ? "+" : ""}${avgWinAssetPct.toFixed(1)}%`;
    const expAssetPct = expR * 1.0;
    const expRounded = Number(expAssetPct.toFixed(2));
    const expStr = `${expRounded > 0 ? "+" : ""}${expAssetPct.toFixed(2)}%`;
    const expCls = expRounded > 0 ? "chg-up" : (expRounded < 0 ? "chg-down" : "muted");
    return {
      available: true,
      winRateInt,
      avgWinR,
      avgWinStr,
      lossStr: "−1%",
      expR,
      expStr,
      expCls,
      copyStatsLine: `策略歷史勝率 ${winRateInt}%｜贏時平均 ${avgWinStr}｜輸時 −1%`,
    };
  }

  function evaluateOpenNowEligibility(tradeObj, currentPrice) {
    if (!tradeObj || tradeObj.state !== "active") {
      return { eligible: false, reason: "not_active" };
    }
    if (!OPEN_NOW_STRATEGIES.has(tradeObj.strategy)) {
      return { eligible: false, reason: "unsupported_strategy" };
    }
    const dir = tradeObj.direction;
    if (dir !== "long" && dir !== "short") {
      return { eligible: false, reason: "invalid_direction" };
    }
    const entryPx = Number(tradeObj.avg_entry_price || tradeObj.entry_price || 0);
    const slPx = Number(tradeObj.current_stop_loss ?? tradeObj.initial_stop_loss ?? tradeObj.stop_loss ?? 0);
    if (!Number.isFinite(entryPx) || entryPx <= 0 || !Number.isFinite(slPx) || slPx <= 0) {
      return { eligible: false, reason: "invalid_price" };
    }
    const rDist = Math.abs(entryPx - slPx);
    if (rDist <= 0) {
      return { eligible: false, reason: "zero_stop_distance" };
    }
    const hasLivePrice =
      currentPrice !== null &&
      currentPrice !== undefined &&
      currentPrice !== "" &&
      !Number.isNaN(Number(currentPrice)) &&
      Number.isFinite(Number(currentPrice));
    if (hasLivePrice) {
      const cur = Number(currentPrice);
      if (dir === "long") {
        if (cur <= slPx) return { eligible: false, reason: "hit_stop" };
        if (cur > entryPx + 0.5 * rDist) return { eligible: false, reason: "exceeded_half_r" };
      } else {
        if (cur >= slPx) return { eligible: false, reason: "hit_stop" };
        if (cur < entryPx - 0.5 * rDist) return { eligible: false, reason: "exceeded_half_r" };
      }
    }
    return {
      eligible: true,
      hasLivePrice,
      curPrice: hasLivePrice ? Number(currentPrice) : null,
      entryPx,
      slPx,
      rDist,
    };
  }

  function computeOpenNowSizing(entryPx, stopLossPx, leverage = SUGGESTED_LEVERAGE) {
    const entry = Number(entryPx);
    const sl = Number(stopLossPx);
    if (!Number.isFinite(entry) || entry <= 0 || !Number.isFinite(sl) || sl <= 0) {
      return {
        notionalPct: 0,
        marginPct: 0,
        lossPct: 0,
        marginPctStr: "0%",
        lossPctStr: "0%",
      };
    }
    const stopDistRatio = Math.abs(entry - sl) / entry;
    if (!Number.isFinite(stopDistRatio) || stopDistRatio <= 0) {
      return {
        notionalPct: 500,
        marginPct: 500 / leverage,
        lossPct: 0,
        marginPctStr: formatPercentDisplay(500 / leverage),
        lossPctStr: "0%",
      };
    }
    // notionalPct = 1% ÷ (|entry−stop| / entry)，上限 500%（沿用現有公式）。
    const notionalPct = Math.min(500, 1 / stopDistRatio);
    // marginPct = notionalPct ÷ SUGGESTED_LEVERAGE。
    const marginPct = notionalPct / leverage;
    // lossPct = notionalPct × |entry−stop| / entry（通常 1%，被上限截斷時用實際值）。
    const lossPct = notionalPct * stopDistRatio;

    return {
      notionalPct,
      marginPct,
      lossPct,
      marginPctStr: formatPercentDisplay(marginPct),
      lossPctStr: formatPercentDisplay(lossPct),
    };
  }

  function computeOpenNowPositionPct(entryPx, stopLossPx) {
    const sizing = computeOpenNowSizing(entryPx, stopLossPx, SUGGESTED_LEVERAGE);
    const posPct = Math.round(sizing.notionalPct);
    const showLeverage = posPct > 100;
    const leverage = (posPct / 100).toFixed(1);
    return { posPct, leverage, showLeverage, ...sizing };
  }

  function formatOpenNowSingleCopyText(card, includeUrl = true) {
    const slLine = `止損：${card.slStr}（打到停損，預計虧損約總資產的 ${card.lossPctStr}）`;
    const sugLine = `建議：開 ${card.suggestedLeverage || SUGGESTED_LEVERAGE} 倍槓桿，用總資產的 ${card.marginPctStr} 當保證金`;
    const histLine = card.historyStats && card.historyStats.available
      ? card.historyStats.copyStatsLine
      : "策略歷史數據暫無";
    const tpLine = includeUrl
      ? `止盈：移動停利，詳見 ${SITE_URL}`
      : "止盈：移動停利";
    return [
      `【${card.dirZh}】${card.displayName}（${card.strategyLabel}）`,
      `入場：${card.entryStr}`,
      slLine,
      sugLine,
      histLine,
      tpLine,
    ].join("\n");
  }

  function formatOpenNowAllCopyText(cards, nowDate) {
    if (!Array.isArray(cards) || cards.length === 0) return "";
    const stamp = formatTaipeiTime(nowDate || new Date());
    const header = `📊 Signal Desk 可開單（${stamp} 台北時間）`;
    const blocks = cards.map((c) => formatOpenNowSingleCopyText(c, false));
    return `${header}\n\n${blocks.join("\n\n")}\n\n詳見 ${SITE_URL}`;
  }

  function buildOpenNowCardsData(tradesList, backtestObj, priceLookupFn, instrumentsList) {
    const cards = [];
    for (const t of (Array.isArray(tradesList) ? tradesList : [])) {
      const curPx = typeof priceLookupFn === "function" ? priceLookupFn(t?.symbol) : getDisplayPrice(t?.symbol).price;
      const elig = evaluateOpenNowEligibility(t, curPx);
      if (!elig.eligible) continue;
      const isLong = t.direction === "long";
      const sizing = computeOpenNowSizing(elig.entryPx, elig.slPx, SUGGESTED_LEVERAGE);
      const historyStats = computeOpenNowHistoryStats(t.strategy, backtestObj);
      const historyLine = historyStats.available ? historyStats.copyStatsLine : "策略歷史數據暫無";
      const { entryStr, slStr } = formatCardPricePair(elig.entryPx, elig.slPx);
      const liveStr = elig.hasLivePrice ? formatCardPrice(elig.curPrice) : null;
      const card = {
        signal_id: t.signal_id,
        strategy: t.strategy,
        symbol: t.symbol,
        isLong,
        dot: isLong ? "🟢" : "🔴",
        dirZh: isLong ? "做多" : "做空",
        displayName: formatSimpleSymbolName(t.symbol, t.market, instrumentsList),
        strategyLabel: getSimpleStrategyShortName(t.strategy, backtestObj),
        entryStr,
        slStr,
        livePriceText: liveStr ? `現價 ${liveStr}` : "現價暫無",
        suggestedLeverage: SUGGESTED_LEVERAGE,
        notionalPct: sizing.notionalPct,
        marginPct: sizing.marginPct,
        lossPct: sizing.lossPct,
        marginPctStr: sizing.marginPctStr,
        lossPctStr: sizing.lossPctStr,
        historyStats,
        historyLine,
        enteredAtMs: Date.parse(t.entered_at_utc || t.published_at_utc || "") || 0,
      };
      card.copyText = formatOpenNowSingleCopyText(card, true);
      cards.push(card);
    }
    cards.sort((a, b) => (b.enteredAtMs - a.enteredAtMs) || String(b.signal_id || "").localeCompare(String(a.signal_id || "")));
    return cards;
  }

  function renderOpenNowSignals() {
    const grid = document.getElementById("simple-open-now-grid");
    const copyAllBtn = document.getElementById("simple-copy-all-btn");
    if (!grid) return;
    const cards = buildOpenNowCardsData(
      cachedTrades,
      cachedBacktest,
      (sym) => getDisplayPrice(sym).price,
      cachedInstruments
    );
    openNowCopyById = {};
    cards.forEach((c) => { openNowCopyById[c.signal_id] = c.copyText; });
    openNowAllCopyText = formatOpenNowAllCopyText(cards);
    if (copyAllBtn) {
      copyAllBtn.disabled = cards.length === 0;
    }
    if (cards.length === 0) {
      grid.innerHTML = '<div class="empty-state">目前沒有可以新開的單，等下一個訊號</div>';
      return;
    }
    const nowMs = Date.now();
    grid.innerHTML = cards
      .map((c) => {
        const recentCopied = openNowCopiedAt[c.signal_id] && (nowMs - openNowCopiedAt[c.signal_id] < 2000);
        const copyLabel = recentCopied ? "已複製 ✓" : "複製";
        return `
          <article class="simple-card open-now-card">
            <div class="open-now-head">
              <div class="open-now-product">
                <span>${c.dot} ${escapeHtml(c.displayName)}</span>
                <span class="pill ${c.isLong ? "pill-ok" : "pill-fail"}">${c.dirZh}</span>
                <span class="open-now-strategy">${escapeHtml(c.strategyLabel)}</span>
              </div>
              <button class="btn btn-xs open-now-copy-btn" data-signal-id="${escapeHtml(c.signal_id)}" type="button">${copyLabel}</button>
            </div>
            <div class="simple-card-line">入場：<strong>${escapeHtml(c.entryStr)}</strong><span class="muted open-now-live">${escapeHtml(c.livePriceText)}</span></div>
            <div class="simple-card-line">止損：<strong>${escapeHtml(c.slStr)}</strong>（打到停損，預計虧損約總資產的 ${escapeHtml(c.lossPctStr)}）</div>
            <div class="simple-card-line">建議：開 ${c.suggestedLeverage || SUGGESTED_LEVERAGE} 倍槓桿，用總資產的 ${escapeHtml(c.marginPctStr)} 當保證金</div>
            <div class="open-now-history">${escapeHtml(c.historyLine)}</div>
            <div class="simple-card-line muted" style="font-size:0.82rem;margin-top:2px;">止盈：移動停利</div>
          </article>
        `;
      })
      .join("");
  }

  function renderWallSummary() {
    const todayStr = new Date().toISOString().slice(0, 10);
    const todayCount = cachedSignals.filter((s) => (s.published_at_utc || "").startsWith(todayStr)).length;
    const activeCount = cachedSignals.filter((s) => s.lifecycle_state === "active").length;

    const cutoff30 = Date.now() - 30 * 86400 * 1000;
    const closed30 = cachedSignals.filter((s) => {
      if (s.lifecycle_state !== "closed") return false;
      const t = new Date(s.closed_at_utc || s.published_at_utc).getTime();
      return !Number.isNaN(t) && t >= cutoff30;
    });

    document.getElementById("sum-today").textContent = String(todayCount);
    document.getElementById("sum-active").textContent = String(activeCount);

    const winEl = document.getElementById("sum-winrate");
    const avgREl = document.getElementById("sum-avgr");
    if (closed30.length < 30) {
      winEl.textContent = closed30.length === 0 ? "樣本不足 (0)" : `樣本不足 (${closed30.length}/30)`;
      avgREl.textContent = closed30.length === 0 ? "樣本不足 (0)" : `樣本不足 (${closed30.length}/30)`;
    } else {
      const wins = closed30.filter((s) => Number(s.r_multiple) > 0).length;
      const avgR = closed30.reduce((acc, s) => acc + Number(s.r_multiple || 0), 0) / closed30.length;
      winEl.textContent = `${((wins / closed30.length) * 100).toFixed(1)}%`;
      avgREl.textContent = `${avgR.toFixed(2)}R`;
    }
  }

  function renderWallCards() {
    renderWallSummary();
    const container = document.getElementById("wall-cards");
    if (!container) return;
    const fStrat = document.getElementById("wall-filter-strategy").value;
    const fMkt = document.getElementById("wall-filter-market").value;
    const fDir = document.getElementById("wall-filter-direction").value;
    const fSym = document.getElementById("wall-filter-symbol").value.trim().toUpperCase();
    const cutoff30 = Date.now() - 30 * 86400 * 1000;

    const tradeById = {};
    for (const t of (Array.isArray(cachedTrades) ? cachedTrades : [])) {
      if (t && t.signal_id) tradeById[t.signal_id] = t;
    }

    const filtered = cachedSignals.filter((s) => {
      if (currentWallTab === "active" && s.lifecycle_state !== "active") return false;
      if (currentWallTab === "closed30") {
        if (s.lifecycle_state !== "closed" && s.lifecycle_state !== "cancelled") return false;
        const t = new Date(s.closed_at_utc || s.published_at_utc).getTime();
        if (Number.isNaN(t) || t < cutoff30) return false;
      }
      if (fStrat !== "all" && s.strategy !== fStrat) return false;
      if (fMkt !== "all" && getUiMarketKey(s.market, s.symbol) !== fMkt) return false;
      if (fDir !== "all" && s.direction !== fDir) return false;
      if (fSym && !s.symbol.toUpperCase().includes(fSym)) return false;
      return true;
    });

    if (filtered.length === 0) {
      container.innerHTML = '<div class="empty-state">目前此分類沒有符合條件的報單（沒有假報單；系統僅於策略與風控全數通過時發布）。</div>';
      return;
    }

    const enriched = filtered.map((s) => {
      const tradeObj = tradeById[s.signal_id] || null;
      const fullSig = cachedSignalById[s.signal_id] || s;
      const merged = {
        ...s,
        ...(tradeObj || {}),
        reasons: fullSig?.reasons || s.reasons,
        verification_status: fullSig?.verification_status || s.verification_status,
        trailing_exit_price: tradeObj?.trailing_exit_price ?? fullSig?.trailing_exit_price ?? s.trailing_exit_price,
        current_stop_loss: tradeObj?.current_stop_loss ?? fullSig?.current_stop_loss ?? s.current_stop_loss ?? s.stop_loss,
        remaining_qty: tradeObj?.remaining_qty ?? fullSig?.remaining_qty ?? s.remaining_qty,
        planned_notional_usd: tradeObj?.planned_notional_usd ?? s.planned_notional_usd ?? s.effective_notional_usd ?? s.notional_usd,
        avg_entry_price: Number(tradeObj?.avg_entry_price || fullSig?.avg_entry_price || s.avg_entry_price || s.entry_price || s.entry_low || 0),
        realized_pnl_usd: tradeObj?.realized_pnl_usd ?? s.realized_pnl_usd ?? 0,
      };
      const verified = isStrategyVerified(s.strategy, merged, cachedBacktest);
      const entryPx = Number(merged.avg_entry_price || 0);
      const est = computeCardEstimates(merged);
      const notionalRaw = Number(merged.planned_notional_usd || est.notional || (Number(merged.remaining_qty || 0) * entryPx) || 0);
      const enteredAtMs = Date.parse(s.entered_at_utc || s.published_at_utc || "") || 0;
      const closedAtMs = Date.parse(s.closed_at_utc || s.published_at_utc || "") || 0;
      return { s, merged, verified, est, notionalRaw, enteredAtMs, closedAtMs };
    });

    if (currentWallTab === "active") {
      enriched.sort((a, b) => (Number(b.verified) - Number(a.verified)) || (b.notionalRaw - a.notionalRaw) || (b.enteredAtMs - a.enteredAtMs));
    } else {
      enriched.sort((a, b) => b.closedAtMs - a.closedAtMs);
    }

    container.innerHTML = enriched
      .map(({ s, merged, verified, est, notionalRaw }) => {
        const isLong = s.direction === "long";
        const dot = isLong ? "🟢" : "🔴";
        const dirZh = isLong ? "做多" : "做空";
        const dirBadge = isLong ? '<span class="pill pill-ok">做多</span>' : '<span class="pill pill-fail">做空</span>';
        const displayName = formatSimpleSymbolName(s.symbol, s.market, cachedInstruments);
        const poolKind = getStrategyPoolKind(s.strategy);
        const pxInfo = getDisplayPrice(s.symbol);
        const liveStr = pxInfo.price !== null ? formatNumber(pxInfo.price) : "--";
        const floatPnl = computeFloatingPnl(merged, pxInfo.price);
        const curSl = merged.current_stop_loss ?? s.stop_loss;
        const entryPx = Number(merged.avg_entry_price || 0);
        const entryStr = entryPx > 0 ? (formatNumber(entryPx) || String(entryPx)) : "—";
        const slStr = curSl > 0 ? (formatNumber(curSl) || String(curSl)) : "—";
        const enteredTp = formatTaipeiTime(s.entered_at_utc || s.published_at_utc);
        const ruleTxt = getStrategyExitRuleText(merged);
        const reasonLine = `${extractSimpleReasonText(merged)}（${s.strategy} ${poolKind}，${verified ? "已驗證" : "觀察中"}）`;
        const marginUsd = Math.round(notionalRaw / 5);
        const notionalUsd = Math.round(notionalRaw);
        const posText = notionalUsd > 0
          ? `本金 $${marginUsd.toLocaleString("en-US")} × 5 倍 = $${notionalUsd.toLocaleString("en-US")}`
          : "—";

        const isClosed = s.lifecycle_state === "closed" || s.lifecycle_state === "cancelled";
        const exitInfo = computeSimpleExitPoint(merged);
        const exitStr = exitInfo.price > 0
          ? `${formatNumber(exitInfo.price) ?? "—"}${exitInfo.suffix}`
          : `—${exitInfo.suffix}`;

        let compactExitLabel = "停利 / 出場";
        let compactExitVal = exitStr;
        let compactPnlLabel = "目前損益";
        let compactPnlVal = "—";
        let compactPnlCls = "muted";
        let liveHtml = "";

        if (isClosed) {
          const rVal = Number(s.r_multiple || 0);
          const rCls = rVal > 0 ? "chg-up" : rVal < 0 ? "chg-down" : "muted";
          const exitLbl = s.exit_reason_label || exitReasonLabel(s.exit_reason);
          compactExitLabel = "出場結果";
          compactExitVal = exitLbl;
          compactPnlLabel = "已實現損益";
          compactPnlVal = `${rVal.toFixed(2)}R / ${formatUsd(s.realized_pnl_usd)}`;
          compactPnlCls = rCls;
          liveHtml = `
            <div class="live-box">
              <div><strong>實際損益（fills 加總）：</strong><span class="${rCls}">${rVal.toFixed(2)}R / ${formatUsd(s.realized_pnl_usd)}</span></div>
              <div class="muted">出場原因：${escapeHtml(exitLbl)}｜結束時間：台北 ${formatTaipeiTime(s.closed_at_utc || s.published_at_utc)}</div>
            </div>`;
        } else {
          const pnlInfo = Number(merged.remaining_qty || 0) > 0 && entryPx > 0
            ? computeSimpleTradePnl(merged, pxInfo.price)
            : (floatPnl
                ? {
                    text: `${floatPnl.usd >= 0 ? "+" : "-"}$${Math.abs(Math.round(floatPnl.usd)).toLocaleString("en-US")}`,
                    cls: floatPnl.usd > 0 ? "chg-up" : floatPnl.usd < 0 ? "chg-down" : "muted",
                  }
                : { text: "—", cls: "muted" });
          compactPnlVal = pnlInfo.text;
          compactPnlCls = pnlInfo.cls;
          const floatTxt = floatPnl
            ? `<div><strong>即時浮動損益：</strong><span class="${floatPnl.usd >= 0 ? "chg-up" : "chg-down"}">${floatPnl.r >= 0 ? "+" : ""}${floatPnl.r.toFixed(2)}R (${formatUsd(floatPnl.usd)})</span></div>`
            : "";
          const trailLine = merged.trailing_exit_price
            ? `<div>目前移動出場線：${formatNumber(merged.trailing_exit_price)}</div>`
            : "";
          liveHtml = `
            <div class="live-box">
              <div><strong>參考現價：</strong>${liveStr} <span class="pill ${pxInfo.failed ? "pill-fail" : "pill-live"}">${pxInfo.source}</span></div>
              <div>距止損：${formatDist(pxInfo.price, curSl)}</div>
              ${trailLine}
              ${floatTxt}
              <div class="muted">即時價格估算，僅供顯示；狀態與成績以引擎已收盤 K 線為準</div>
            </div>`;
        }

        const verLabel = s.verification_status || (verified ? "已驗證" : "觀察中（未通過驗證）");
        const verCls = verLabel === "已驗證" ? "pill-ok" : "pill-warn";
        const isExpanded = expandedWallSignalIds.has(s.signal_id);

        return `
          <article class="signal-card${isExpanded ? " expanded" : ""}" data-sid="${escapeHtml(s.signal_id)}">
            <div class="signal-card-compact" role="button" tabindex="0" aria-expanded="${isExpanded ? "true" : "false"}">
              <div class="signal-compact-head">
                <div class="signal-compact-title">
                  <span>${dot} ${escapeHtml(displayName)}</span>
                  ${dirBadge}
                  <span class="pill pill-neutral">${escapeHtml(s.strategy)} ${escapeHtml(poolKind)}</span>
                </div>
                <div class="signal-compact-badges">
                  <span class="pill ${verCls}">${escapeHtml(verLabel)}</span>
                  <span class="pill pill-live">${lifecycleLabel(s.lifecycle_state)}</span>
                  <button class="btn btn-xs wall-expand-btn" type="button" aria-expanded="${isExpanded ? "true" : "false"}">${isExpanded ? "收合 ▴" : "展開 ▾"}</button>
                </div>
              </div>
              <div class="signal-compact-reason muted">理由：${escapeHtml(reasonLine)}</div>
              <div class="signal-compact-kv">
                <div><span class="muted">進場價：</span><strong>${escapeHtml(entryStr)}</strong></div>
                <div><span class="muted">停損價：</span><strong>${escapeHtml(slStr)}</strong></div>
                <div><span class="muted">${compactExitLabel}：</span><strong>${escapeHtml(compactExitVal)}</strong></div>
                <div><span class="muted">${compactPnlLabel}：</span><strong class="${compactPnlCls}">${escapeHtml(compactPnlVal)}</strong></div>
                <div class="signal-compact-kv-wide"><span class="muted">倉位：</span>${escapeHtml(posText)}</div>
              </div>
            </div>
            <div class="signal-card-details">
              <div class="signal-card-head">
                <div>
                  <div class="signal-card-title">${escapeHtml(formatInstrumentTitle(s.symbol))} ${dirBadge}</div>
                  <div class="muted">${escapeHtml(s.signal_id)}｜${STRATEGY_NAMES[s.strategy] || `策略 ${s.strategy}`}｜台北 ${enteredTp}</div>
                </div>
              </div>
              <div class="badge badge-warn" style="align-self:flex-start;">模擬驗證中，非投資建議</div>
              <div class="signal-kv">
                <div><span class="muted">進場：</span>市價 ${escapeHtml(entryStr)}（台北 ${enteredTp} 成交）</div>
                <div><span class="muted">倉位名目：</span>$${fmtCompactUsd(est.notional)}（佔 ${escapeHtml(s.strategy)} 資金 ${est.notionalPct.toFixed(1)}%）</div>
                <div><span class="muted">止損：</span>${escapeHtml(slStr)}</div>
                <div><span class="muted">預估 SL 虧損：</span><span class="chg-down">-$${fmtCompactUsd(est.slUsd)}（佔 ${escapeHtml(s.strategy)} 資金 ${est.slPct.toFixed(1)}%）</span></div>
                <div style="grid-column:1 / -1;"><span class="muted">出場規則：</span>${escapeHtml(ruleTxt)}</div>
              </div>
              ${liveHtml}
              ${renderSignalRelatedNewsHtml(s.symbol)}
              <div class="card-actions">
                <button class="btn copy-sig-btn" data-sid="${escapeHtml(s.signal_id)}" type="button">一鍵複製報單文字</button>
                <a class="btn btn-primary" href="#/signal/${encodeURIComponent(s.signal_id)}">查看詳情</a>
              </div>
            </div>
          </article>`;
      })
      .join("");
  }

  async function renderSignalDetail(signalId) {
    const root = document.getElementById("signal-detail-root");
    root.innerHTML = '<div class="card muted">載入報單詳情中...</div>';

    let sig = cachedSignals.find((s) => s.signal_id === signalId) || null;
    if (!sig) {
      try {
        sig = await fetchDataJson(`signals/${encodeURIComponent(signalId)}.json`);
      } catch (err) {
        root.innerHTML = `<div class="card"><p class="error-box">找不到報單 ${signalId}（${err.message}）</p><a class="btn" href="#/wall">返回訊號與報單</a></div>`;
        return;
      }
    }

    const month = (sig.published_at_utc || "").slice(0, 7);
    const [snapRes, updRes, chartRes] = await Promise.allSettled([
      fetchDataJson(`snapshots/${encodeURIComponent(sig.snapshot_id)}.json`),
      month ? fetchDataText(`signal_updates/${month}.jsonl`) : Promise.reject(new Error("no month")),
      fetchDataJson(`charts/${encodeURIComponent(sig.signal_id)}.json`),
    ]);

    const snapshot = snapRes.status === "fulfilled" ? snapRes.value : null;
    let updates = [];
    if (updRes.status === "fulfilled" && updRes.value) {
      updates = updRes.value
        .split("\n")
        .map((l) => l.trim())
        .filter(Boolean)
        .map((l) => { try { return JSON.parse(l); } catch (_) { return null; } })
        .filter((ev) => ev && ev.signal_id === sig.signal_id);
    }
    const chartData = chartRes.status === "fulfilled" ? chartRes.value : null;

    const pxInfo = getDisplayPrice(sig.symbol);
    const floatPnl = computeFloatingPnl(sig, pxInfo.price);
    const curSl = sig.current_stop_loss ?? sig.stop_loss;
    const entryPx = Number(sig.avg_entry_price || sig.entry_price || sig.entry_low || 0);
    const enteredTp = formatTaipeiTime(sig.entered_at_utc || sig.published_at_utc);
    const ruleTxt = getStrategyExitRuleText(sig);
    const exitLbl = sig.exit_reason ? (sig.exit_reason_label || exitReasonLabel(sig.exit_reason)) : "--";

    const reasonsHtml = Array.isArray(sig.reasons)
      ? sig.reasons.map((r) => `<li>${escapeHtml(convertReasonUtcToTaipei(r))}</li>`).join("")
      : '<li class="muted">無</li>';

    const updatesHtml = updates.length > 0
      ? `<ul class="timeline">${updates.map((u) => `<li class="timeline-item"><strong>台北 ${formatTaipeiTime(u.ts_utc)}</strong>｜<code>${escapeHtml(u.event_type)}</code> @ ${formatNumber(u.price) || u.price}<br>${escapeHtml(convertReasonUtcToTaipei(u.description))}</li>`).join("")}</ul>`
      : '<p class="muted">尚無更新事件。</p>';

    const snapHtml = snapshot
      ? `<pre style="overflow-x:auto;font-size:0.8rem;">${escapeHtml(convertReasonUtcToTaipei(JSON.stringify({ kline_summary: snapshot.kline_summary, indicators: snapshot.indicators, filter_checks: snapshot.filter_checks }, null, 2)))}</pre>`
      : '<p class="muted">無快照資料。</p>';

    const verDetailLabel = sig.verification_status || "觀察中（未通過驗證）";
    const verDetailCls = verDetailLabel === "已驗證" ? "pill-ok" : "pill-warn";
    const sigEst = computeCardEstimates(sig);

    root.innerHTML = `
      <section class="card">
        <div class="section-header">
          <div>
            <h1>${sig.signal_id}｜${sig.symbol}（${sig.direction === "long" ? "做多" : "做空"}）</h1>
            <p class="subtitle">${STRATEGY_NAMES[sig.strategy] || `策略 ${sig.strategy}`}｜<span class="pill ${verDetailCls}">${escapeHtml(verDetailLabel)}</span>｜狀態：<strong>${lifecycleLabel(sig.lifecycle_state)}</strong>｜進場時間：台北 ${enteredTp}</p>
          </div>
          <div class="card-actions">
            <a class="btn" href="#/wall">返回訊號與報單</a>
            <button id="detail-copy-url" class="btn" type="button">複製固定網址</button>
            <button id="detail-copy-text" class="btn btn-primary" type="button">一鍵複製報單文字</button>
          </div>
        </div>
        <div class="badge badge-warn">模擬驗證中，非投資建議</div>
        <div class="live-box" style="margin-top:12px;">
          <div><strong>瀏覽器參考現價：</strong>${pxInfo.price !== null ? formatNumber(pxInfo.price) : "--"}（${pxInfo.source}）</div>
          <div>距止損 (${formatNumber(curSl) || curSl})：${formatDist(pxInfo.price, curSl)}${sig.trailing_exit_price ? `｜目前移動出場線：${formatNumber(sig.trailing_exit_price)}` : ""}</div>
          ${floatPnl ? `<div><strong>即時浮動損益：</strong>${floatPnl.r >= 0 ? "+" : ""}${floatPnl.r.toFixed(2)}R / ${formatUsd(floatPnl.usd)}</div>` : ""}
          <div class="muted">即時價格估算，僅供顯示；報單狀態與模擬成績一律以引擎已收盤 K 線為準</div>
        </div>
      </section>

      <section class="card">
        <h2>K 線圖與計畫點位</h2>
        <div id="signal-chart-box" class="chart-container"></div>
        <p id="chart-note" class="muted" style="margin-top:6px;">標示進場價、止損價與移動出場線。</p>
      </section>

      <section class="card">
        <h2>原始計畫 vs 目前狀態對照</h2>
        <div class="table-wrap">
          <table class="data-table">
            <thead><tr><th>項目</th><th>進場計畫 / 預估</th><th>目前狀態</th></tr></thead>
            <tbody>
              <tr><td>進場市價 / 成交時間</td><td>${formatNumber(entryPx) || entryPx}</td><td>台北 ${enteredTp}</td></tr>
              <tr><td>固定止損價</td><td>${formatNumber(sig.stop_loss) || sig.stop_loss}</td><td>${formatNumber(curSl) || curSl}</td></tr>
              <tr><td>倉位名目 / 預估 SL 虧損</td><td>$${fmtCompactUsd(sigEst.notional)}（佔 ${sig.strategy} 資金 ${sigEst.notionalPct.toFixed(1)}%）</td><td class="chg-down">-$${fmtCompactUsd(sigEst.slUsd)}（佔 ${sig.strategy} 資金 ${sigEst.slPct.toFixed(1)}%）</td></tr>
              <tr><td>出場規則</td><td colspan="2">${escapeHtml(ruleTxt)}</td></tr>
              <tr><td>已實現損益 / 出場結果</td><td>${formatUsd(sig.realized_pnl_usd)} (${sig.r_multiple}R)</td><td>出場原因：${escapeHtml(exitLbl)}${sig.closed_at_utc ? `（台北 ${formatTaipeiTime(sig.closed_at_utc)}）` : ""}</td></tr>
            </tbody>
          </table>
        </div>
      </section>

      <section class="card">
        <h2>更新事件時間軸</h2>
        ${updatesHtml}
      </section>

      <section class="card">
        <h2>報單理由與輸入快照摘要</h2>
        <ul>${reasonsHtml}</ul>
        ${snapHtml}
      </section>
      ${renderSignalRelatedNewsHtml(sig.symbol)}
    `;

    bindCopyButton(document.getElementById("detail-copy-url"), window.location.href, "複製固定網址");
    bindCopyButton(document.getElementById("detail-copy-text"), formatSignalCopyText(sig), "一鍵複製報單文字");

    renderSignalChart(sig, chartData);
  }

  function renderSignalChart(sig, chartData) {
    const box = document.getElementById("signal-chart-box");
    const note = document.getElementById("chart-note");
    if (!box) return;
    if (activeChart) {
      try { activeChart.remove(); } catch (_) {}
      activeChart = null;
    }
    if (!window.LightweightCharts || !chartData || !Array.isArray(chartData.bars) || chartData.bars.length === 0) {
      box.innerHTML = '<div class="empty-state">尚無該報單之圖表 K 線快取（將於排程更新時寫入）。</div>';
      return;
    }
    const chart = window.LightweightCharts.createChart(box, {
      layout: { background: { color: "#0d1117" }, textColor: "#8b949e" },
      grid: { vertLines: { color: "#21262d" }, horzLines: { color: "#21262d" } },
      width: box.clientWidth || 600,
      height: box.clientHeight || 340,
      timeScale: { timeVisible: true, secondsVisible: false },
    });
    activeChart = chart;
    const samplePx = Number(chartData.bars[0]?.close || sig.avg_entry_price || sig.entry_price || 1);
    const chartPrec = getPricePrecision(samplePx);
    const series = chart.addCandlestickSeries({
      upColor: "#2ea043",
      downColor: "#f85149",
      borderVisible: false,
      wickUpColor: "#2ea043",
      wickDownColor: "#f85149",
      priceFormat: { type: "price", precision: chartPrec, minMove: Math.pow(10, -chartPrec) },
    });

    const candles = chartData.bars.map((b) => ({
      time: Math.floor(new Date(b.ts_utc).getTime() / 1000),
      open: Number(b.open),
      high: Number(b.high),
      low: Number(b.low),
      close: Number(b.close),
    }));
    series.setData(candles);

    const addLine = (price, color, title) => {
      if (typeof price === "number" && !Number.isNaN(price) && price > 0) {
        series.createPriceLine({ price, color, lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title });
      }
    };
    const entryPx = Number(sig.avg_entry_price || sig.entry_price || sig.entry_low || 0);
    addLine(entryPx, "#58a6ff", "進場");
    addLine(Number(sig.current_stop_loss ?? sig.stop_loss), "#f85149", "止損");
    if (typeof sig.trailing_exit_price === "number") addLine(Number(sig.trailing_exit_price), "#2ea043", "移動出場線");

    if (sig.entered_at_utc) {
      const tSec = Math.floor(new Date(sig.entered_at_utc).getTime() / 1000);
      if (!Number.isNaN(tSec)) {
        series.setMarkers([
          {
            time: tSec,
            position: sig.direction === "long" ? "belowBar" : "aboveBar",
            color: "#79c0ff",
            shape: sig.direction === "long" ? "arrowUp" : "arrowDown",
            text: `進場 @ ${formatNumber(entryPx) || entryPx}`,
          },
        ]);
      }
    }
    chart.timeScale().fitContent();
    if (note) note.textContent = `圖表週期：${chartData.interval}（已標示進場、止損與移動出場線）`;
  }

  function renderPriceCell(item) {
    const schedStr = formatNumber(item.latest_price);
    const live = livePrices[item.symbol];
    if (isLiveCapable(item.symbol)) {
      if (live && !live.failed && live.price !== undefined) {
        const liveStr = formatNumber(live.price);
        const schedSub = schedStr ? `<br><span class="muted">排程價：${schedStr}</span>` : "";
        return `<strong>${liveStr}</strong> <span class="pill pill-live">即時 ${live.source}</span>${schedSub}`;
      }
      if (live && live.failed) {
        const fallback = schedStr || '<span class="pill pill-fail">資料來源失敗</span>';
        return `${fallback} <span class="pill pill-fail">即時價格來源失敗</span>`;
      }
    }
    if (!schedStr) return '<span class="pill pill-fail">資料來源失敗</span>';
    return `${schedStr} <span class="muted">(僅排程)</span>`;
  }

  function formatPriceWithCommas(val, forcedDecimals) {
    if (val === null || val === undefined || val === "" || Number.isNaN(Number(val))) return "—";
    const num = Number(val);
    if (!Number.isFinite(num)) return "—";
    const dec = Number.isInteger(forcedDecimals) && forcedDecimals >= 0
      ? forcedDecimals
      : getCardPriceDecimals(num);
    const fixedStr = num.toFixed(dec);
    const parts = fixedStr.split(".");
    parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    return parts.join(".");
  }

  function shortenSrAlgo(algoStr) {
    if (!algoStr) return "參考位";
    const first = String(algoStr).split(/[；／]/)[0].trim();
    return first || "參考位";
  }

  function formatSrStars(strength) {
    const n = Math.max(1, Math.min(3, Number(strength) || 1));
    return "★".repeat(n) + "☆".repeat(3 - n);
  }

  function renderTaPanelHtml(symbol, livePriceOverride, taOverride) {
    const taRoot = taOverride || cachedTa;
    const ta = taRoot && taRoot.symbols ? taRoot.symbols[symbol] : null;
    if (!ta) {
      return '<div class="ta-panel"><div class="ta-empty muted">技術分析資料暫無</div></div>';
    }

    const live = livePrices[symbol];
    const hasLive = livePriceOverride !== undefined && livePriceOverride !== null
      ? true
      : Boolean(live && !live.failed && live.price !== undefined);
    const curPrice = livePriceOverride !== undefined && livePriceOverride !== null
      ? Number(livePriceOverride)
      : (live && !live.failed && live.price !== undefined ? Number(live.price) : Number(ta.latest_price));

    const vol = ta.volatility || {};
    const atr20 = vol.atr20 !== undefined && vol.atr20 !== null ? Number(vol.atr20) : null;

    // Re-bucket historical S/R relative to active curPrice; keep Fibo extensions only when a side has no historical level
    const normalizeSr = (x, fallbackSide) => ({
      price: Number(x.price),
      label: x.label || shortenSrAlgo(x.algo),
      algo: x.algo || "",
      strength: x.is_extension ? 1 : (Number(x.strength) || 1),
      is_extension: Boolean(x.is_extension),
      side: fallbackSide,
    });
    const rawRes = (Array.isArray(ta.resistances) ? ta.resistances : [])
      .filter((x) => x && x.price !== undefined && x.price !== null && Number.isFinite(Number(x.price)))
      .map((x) => normalizeSr(x, "res"));
    const rawSup = (Array.isArray(ta.supports) ? ta.supports : [])
      .filter((x) => x && x.price !== undefined && x.price !== null && Number.isFinite(Number(x.price)))
      .map((x) => normalizeSr(x, "sup"));

    const realLevels = [...rawRes, ...rawSup].filter((x) => !x.is_extension);
    const realRes = realLevels
      .filter((x) => x.price >= curPrice)
      .sort((a, b) => a.price - b.price)
      .slice(0, 3);
    const realSup = realLevels
      .filter((x) => x.price < curPrice)
      .sort((a, b) => b.price - a.price)
      .slice(0, 3);

    const resExt = rawRes.filter((x) => x.is_extension && x.price > curPrice).sort((a, b) => a.price - b.price);
    const supExt = rawSup.filter((x) => x.is_extension && x.price < curPrice).sort((a, b) => b.price - a.price);

    const resNoClear = realRes.length === 0 && (ta.resistance_status === "無明確壓力" || resExt.length > 0);
    const supNoClear = realSup.length === 0 && (ta.support_status === "無明確支撐" || supExt.length > 0);

    const resistances = resNoClear ? resExt.slice(0, 3) : realRes;
    const supports = supNoClear ? supExt.slice(0, 3) : realSup;

    const nearRes = resistances.length > 0 ? resistances[0] : null;
    const nearSup = supports.length > 0 ? supports[0] : null;

    const trend = ta.trend || {};
    const squeeze = ta.squeeze || {};
    const trendState = trend.state || "盤整";
    let trendBadgeCls = "ta-trend-flat";
    if (trendState.includes("上升") || trendState.includes("多頭")) trendBadgeCls = "ta-trend-up";
    else if (trendState.includes("下降") || trendState.includes("空頭")) trendBadgeCls = "ta-trend-down";

    let sqBadgeCls = "ta-sq-normal";
    let sqBadgeLabel = "波動正常";
    if (squeeze.is_squeezing || (squeeze.bb_width_pct !== undefined && squeeze.bb_width_pct !== null && squeeze.bb_width_pct < 20.0)) {
      sqBadgeCls = "ta-sq-squeeze";
      sqBadgeLabel = "收斂中，可能變盤";
    } else if (squeeze.bb_width_pct !== undefined && squeeze.bb_width_pct !== null && squeeze.bb_width_pct >= 80.0) {
      sqBadgeCls = "ta-sq-expand";
      sqBadgeLabel = "擴張中";
    } else if (squeeze.label && squeeze.label.includes("擴張")) {
      sqBadgeCls = "ta-sq-expand";
      sqBadgeLabel = "擴張中";
    }

    const pattern = ta.pattern || {};
    const patternRaw = String(pattern.name || "無明顯型態")
      .replace(/（僅供參考）/g, "")
      .replace(/\(僅供參考\)/g, "")
      .trim();

    const curPxDec = getCardPriceDecimals(curPrice);
    const curStr = formatPriceWithCommas(curPrice, curPxDec);

    const fmtDist = (targetPx) => {
      if (!(curPrice > 0) || targetPx === undefined || targetPx === null) return "--";
      const diffPct = ((Number(targetPx) - curPrice) / curPrice) * 100.0;
      return `${diffPct >= 0 ? "+" : ""}${diffPct.toFixed(1)}%`;
    };

    const resStr = resNoClear
      ? "無明確壓力"
      : (nearRes ? formatPriceWithCommas(nearRes.price) : "無明確壓力");
    const resDistStr = resNoClear
      ? (nearRes ? `延伸參考 ${formatPriceWithCommas(nearRes.price)} (${fmtDist(nearRes.price)})` : "上方無歷史價位")
      : (nearRes ? fmtDist(nearRes.price) : "--");
    const resSrcStr = nearRes ? nearRes.label : (ta.resistance_note || "上方無歷史價位");

    const supStr = supNoClear
      ? "無明確支撐"
      : (nearSup ? formatPriceWithCommas(nearSup.price) : "無明確支撐");
    const supDistStr = supNoClear
      ? (nearSup ? `延伸參考 ${formatPriceWithCommas(nearSup.price)} (${fmtDist(nearSup.price)})` : "下方無歷史價位")
      : (nearSup ? fmtDist(nearSup.price) : "--");
    const supSrcStr = nearSup ? nearSup.label : (ta.support_note || "下方無歷史價位");

    const tom = vol.tomorrow || {};
    const wk = vol.week || {};
    const tomRangeOnly = tom.low !== undefined && tom.high !== undefined
      ? `${formatPriceWithCommas(tom.low)} – ${formatPriceWithCommas(tom.high)}`
      : "暫無";
    const wkRangeOnly = wk.low !== undefined && wk.high !== undefined
      ? `${formatPriceWithCommas(wk.low)} – ${formatPriceWithCommas(wk.high)}`
      : "暫無";
    const rangeEstimateText = `明日約 ${tomRangeOnly} ｜ 本週約 ${wkRangeOnly}`;

    // --- Block 1: S/R Table (top: up to 3 resistances descending, middle: curPrice, bottom: up to 3 supports descending) ---
    const resTableRows = [...resistances]
      .reverse()
      .map(
        (item) => `
          <div class="ta-sr-row is-res" title="${escapeHtml(item.algo || item.label)}">
            <span class="ta-sr-px">${formatPriceWithCommas(item.price)}</span>
            <span class="ta-sr-dist">${escapeHtml(fmtDist(item.price))}</span>
            <span class="ta-sr-src">${escapeHtml(item.label)}</span>
            <span class="ta-sr-stars">${formatSrStars(item.strength)}</span>
          </div>`
      )
      .join("");

    const supTableRows = supports
      .map(
        (item) => `
          <div class="ta-sr-row is-sup" title="${escapeHtml(item.algo || item.label)}">
            <span class="ta-sr-px">${formatPriceWithCommas(item.price)}</span>
            <span class="ta-sr-dist">${escapeHtml(fmtDist(item.price))}</span>
            <span class="ta-sr-src">${escapeHtml(item.label)}</span>
            <span class="ta-sr-stars">${formatSrStars(item.strength)}</span>
          </div>`
      )
      .join("");

    const srCurRow = `
      <div class="ta-sr-row is-cur">
        <span class="ta-sr-px">${curStr}</span>
        <span class="ta-sr-dist">0.0%</span>
        <span class="ta-sr-src">現價</span>
        <span class="ta-sr-stars">—</span>
      </div>`;

    // --- Block 2: MA & Trend ---
    const fmtSmaVal = (val) => {
      if (val === undefined || val === null || Number.isNaN(Number(val))) {
        return '<span class="muted">資料不足</span>';
      }
      const num = Number(val);
      const above = curPrice >= num;
      const arrowCls = above ? "ta-ma-up" : "ta-ma-down";
      const arrowChar = above ? "↑" : "↓";
      return `${formatPriceWithCommas(num)} <span class="${arrowCls}">${arrowChar}</span>`;
    };

    let adxDisplay = "資料不足";
    if (trend.adx14 !== undefined && trend.adx14 !== null && !Number.isNaN(Number(trend.adx14))) {
      const adxNum = Number(trend.adx14);
      const adxPlain = adxNum < 20 ? "無趨勢" : (adxNum <= 40 ? "有趨勢" : "強趨勢");
      adxDisplay = `${adxNum.toFixed(1)}（${adxPlain}）`;
    }

    // --- Block 3: Volatility ---
    let atrDisplay = "資料不足";
    if (atr20 !== null && curPrice > 0) {
      const atrPct = (atr20 / curPrice) * 100.0;
      atrDisplay = `${formatPriceWithCommas(atr20)} (${atrPct.toFixed(1)}%)`;
    }
    let bbDisplay = "資料不足";
    if (squeeze.bb_width_pct !== undefined && squeeze.bb_width_pct !== null && !Number.isNaN(Number(squeeze.bb_width_pct))) {
      const bbNum = Number(squeeze.bb_width_pct);
      const bbPlain = bbNum < 20 ? "收斂中，可能變盤" : (bbNum >= 80 ? "波動擴張中" : "波動正常");
      bbDisplay = `${bbNum.toFixed(1)}%（${bbPlain}）`;
    }

    // --- Block 4: Fibonacci & Pattern ---
    const fib = ta.fibonacci || {};
    const fibLevels = fib.levels || {};
    const fibRangeText = fib.start_price !== undefined && fib.end_price !== undefined
      ? `${escapeHtml(fib.start_date || "")} (${formatPriceWithCommas(fib.start_price)}) → ${escapeHtml(fib.end_date || "")} (${formatPriceWithCommas(fib.end_price)})`
      : "無足夠資料";
    const fmtFibVal = (k) => {
      const v = fibLevels[k];
      if (v === undefined || v === null || Number.isNaN(Number(v))) return "—";
      return `${formatPriceWithCommas(v)} <span class="ta-sub-pct">(${escapeHtml(fmtDist(v))})</span>`;
    };
    const patternCleanNote = String(pattern.note || "")
      .replace(/（僅供參考）/g, "")
      .replace(/\(僅供參考\)/g, "")
      .trim() || "近期無特殊軌跡型態";

    // --- Bottom Historical Validation ---
    const bt = ta.backtest || {};
    const btSupText = bt.support_label || "樣本不足";
    const btResText = bt.resistance_label || "樣本不足";
    const btSummaryText = `歷史驗證（觸及後 5 日）：支撐守住 ${btSupText} ｜ 壓力壓住 ${btResText}`;

    const disclaimerText = (taRoot && taRoot.disclaimer)
      || "以上為依歷史數據與技術指標計算的參考點位，不是預測，也不是投資建議。";

    return `
      <div class="ta-panel" data-ta-symbol="${escapeHtml(symbol)}">
        <div class="ta-top-grid">
          <!-- 左側：重點卡 -->
          <div class="ta-hero-card">
            <div class="ta-hero-badges-row">
              <div class="ta-trend-badge ${trendBadgeCls}">
                <span class="ta-badge-icon">${trendBadgeCls === "ta-trend-up" ? "▲" : (trendBadgeCls === "ta-trend-down" ? "▼" : "■")}</span>
                <span class="ta-badge-text">${escapeHtml(trendState)}</span>
              </div>
              <div class="ta-hero-sub-badges">
                ${ta.updating || ta.data_status === "資料更新中" ? '<span class="ta-sq-badge ta-sq-squeeze">資料更新中</span>' : ""}
                <span class="ta-sq-badge ${sqBadgeCls}">${escapeHtml(sqBadgeLabel)}</span>
                <span class="ta-pattern-badge">${escapeHtml(patternRaw)}<span class="ta-ref-tag">參考</span></span>
              </div>
            </div>

            <div class="ta-big-numbers-grid">
              <div class="ta-num-col is-res">
                <div class="ta-num-label">上方壓力</div>
                <div class="ta-num-val">${resStr}</div>
                <div class="ta-num-sub">${escapeHtml(resDistStr)}</div>
                <div class="ta-num-src">${escapeHtml(resSrcStr)}</div>
              </div>
              <div class="ta-num-col is-cur">
                <div class="ta-num-label">現價</div>
                <div class="ta-num-val ta-val-cur">${curStr}</div>
                <div class="ta-num-sub">${hasLive ? "即時報價" : "日線基準"}</div>
                <div class="ta-num-src">${escapeHtml(ta.latest_bar_date || "")}</div>
              </div>
              <div class="ta-num-col is-sup">
                <div class="ta-num-label">下方支撐</div>
                <div class="ta-num-val">${supStr}</div>
                <div class="ta-num-sub">${escapeHtml(supDistStr)}</div>
                <div class="ta-num-src">${escapeHtml(supSrcStr)}</div>
              </div>
            </div>

            <div class="ta-range-box">
              <span class="ta-range-icon">≈</span>
              <span class="ta-range-text">${escapeHtml(rangeEstimateText)}</span>
            </div>
          </div>

          <!-- 右側：詳細數據卡（2x2 格線） -->
          <div class="ta-detail-panel">
            <div class="ta-detail-header">
              <span class="ta-detail-title">詳細數據${ta.display_name ? ` — ${escapeHtml(ta.display_name)}` : ""}</span>
              <span class="ta-detail-date">${ta.latest_bar_date ? `日線截至 ${escapeHtml(ta.latest_bar_date)}` : ""}</span>
            </div>

            <div class="ta-detail-grid-2x2">
              <!-- 區塊 1：支撐壓力表 -->
              <div class="ta-subcard">
                <h5 class="ta-subcard-title">支撐壓力表</h5>
                <div class="ta-sr-table">
                  ${resNoClear ? '<div class="ta-sr-empty muted">無明確壓力（以下為 Fibonacci 延伸參考）</div>' : ""}
                  ${resTableRows || '<div class="ta-sr-empty muted">無明確壓力</div>'}
                  ${srCurRow}
                  ${supNoClear ? '<div class="ta-sr-empty muted">無明確支撐（以下為 Fibonacci 延伸參考）</div>' : ""}
                  ${supTableRows || '<div class="ta-sr-empty muted">無明確支撐</div>'}
                </div>
              </div>

              <!-- 區塊 2：均線與趨勢 -->
              <div class="ta-subcard">
                <h5 class="ta-subcard-title">均線與趨勢</h5>
                <div class="ta-kv-list">
                  <div class="ta-kv-line"><span>SMA20</span><strong>${fmtSmaVal(trend.sma20)}</strong></div>
                  <div class="ta-kv-line"><span>SMA50</span><strong>${fmtSmaVal(trend.sma50)}</strong></div>
                  <div class="ta-kv-line"><span>SMA200</span><strong>${fmtSmaVal(trend.sma200)}</strong></div>
                  <div class="ta-kv-line"><span>ADX14</span><strong>${escapeHtml(adxDisplay)}</strong></div>
                </div>
              </div>

              <!-- 區塊 3：波動 -->
              <div class="ta-subcard">
                <h5 class="ta-subcard-title">波動</h5>
                <div class="ta-kv-list">
                  <div class="ta-kv-line"><span>ATR20</span><strong>${escapeHtml(atrDisplay)}</strong></div>
                  <div class="ta-kv-line"><span>布林寬度百分位</span><strong>${escapeHtml(bbDisplay)}</strong></div>
                  <div class="ta-kv-line"><span>明日預估</span><strong>${escapeHtml(tomRangeOnly)}</strong></div>
                  <div class="ta-kv-line"><span>本週預估</span><strong>${escapeHtml(wkRangeOnly)}</strong></div>
                </div>
              </div>

              <!-- 區塊 4：斐波那契與型態 -->
              <div class="ta-subcard">
                <h5 class="ta-subcard-title">斐波那契與型態</h5>
                <div class="ta-kv-list">
                  <div class="ta-kv-line"><span>波段起訖</span><span class="ta-kv-wrap">${fibRangeText}</span></div>
                  <div class="ta-kv-line"><span>38.2%</span><strong>${fmtFibVal("38.2%")}</strong></div>
                  <div class="ta-kv-line"><span>50.0%</span><strong>${fmtFibVal("50.0%")}</strong></div>
                  <div class="ta-kv-line"><span>61.8%</span><strong>${fmtFibVal("61.8%")}</strong></div>
                  <div class="ta-kv-line"><span>${escapeHtml(patternRaw)}</span><span class="ta-kv-wrap muted">${escapeHtml(patternCleanNote)}</span></div>
                </div>
              </div>
            </div>
          </div>
        </div>

        <div class="ta-bt-bar">${escapeHtml(btSummaryText)}</div>
        ${symbol === "TWII" && cachedFlowTw && window.SIGNAL_DESK_FLOW_TW ? `<div class="ta-flow-tw-wrap">${window.SIGNAL_DESK_FLOW_TW.renderFlowTwCardHtml(cachedFlowTw, { compact: true, researchHref: "#/research/flow_signals" })}</div>` : ""}
        <div class="ta-disclaimer">${escapeHtml(disclaimerText)}</div>
      </div>`;
  }

  function renderInstruments() {
    const tbody = document.getElementById("instruments-tbody");
    const rows = cachedInstruments.filter((item) => {
      if (!item || item.market === "forex") return false;
      return currentMarketFilter === "all" || getUiMarketKey(item.market, item.symbol, item) === currentMarketFilter;
    });
    if (rows.length === 0) {
      tbody.innerHTML = '<tr><td colspan="9" class="muted center">無符合條件的標的資料</td></tr>';
      return;
    }
    tbody.innerHTML = rows
      .map((item) => {
        const iv = item.intervals || {};
        const r1m = iv["1m"] || {};
        const r15m = iv["15m"] || {};
        const r1d = iv["1d"] || {};
        const note = r15m.reason || r1d.reason || "";
        const live = livePrices[item.symbol];
        const chgHtml = live && !live.failed ? formatChange(live.changePct) : '<span class="muted">--</span>';
        const isExpanded = expandedTaSymbol === item.symbol;
        const rowClass = `instrument-row${isExpanded ? " ta-expanded" : ""}`;
        const detailRowHtml = isExpanded
          ? `<tr class="ta-detail-row" data-symbol="${escapeHtml(item.symbol)}"><td colspan="9" class="ta-detail-cell">${renderTaPanelHtml(item.symbol)}</td></tr>`
          : "";
        return `
          <tr class="${rowClass}" data-symbol="${escapeHtml(item.symbol)}">
            <td data-label="標的"><strong>${item.symbol}</strong><br><span class="muted">${item.display_name || ""}</span></td>
            <td data-label="市場">${marketLabel(item.market, item.symbol, item)}</td>
            <td data-label="最新價格">${renderPriceCell(item)}</td>
            <td data-label="24h 漲跌">${chgHtml}</td>
            <td data-label="排程來源">${item.source_used || '<span class="muted">無</span>'}</td>
            <td data-label="1m">${statusPill(r1m.status)}</td>
            <td data-label="15m">${statusPill(r15m.status)}</td>
            <td data-label="1d">${statusPill(r1d.status)}</td>
            <td data-label="狀態 / 備註">${statusPill(item.overall_status)} <span class="muted">${escapeHtml(note)}</span></td>
          </tr>${detailRowHtml}`;
      })
      .join("");
  }

  function classifyWatchReason(reason) {
    const r = String(reason || "");
    if (/已發布|signal/.test(r)) return { label: "已出單", rank: 0 };
    if (/未觸及|區間上下緣|RSI|入場條件|未達突破|未入選雙動能|未成立/.test(r)) return { label: "未觸及入場條件", rank: 1 };
    if (/ADX|布林|ATR|區間|盤整|趨勢|波動|再平衡/.test(r)) return { label: "市場狀態不符", rank: 2 };
    if (/風控|事件|資金費率|未平倉|排程延遲|暫停/.test(r)) return { label: "風控或事件擋下", rank: 3 };
    if (/資料過期/.test(r)) return { label: "資料過期", rank: 4 };
    if (/資料不穩|資料不足|資料失敗|失敗|異常|延遲/.test(r)) return { label: "資料不穩", rank: 6 };
    return { label: "其他觀望", rank: 5 };
  }

  function formatStrategyJudgments(w) {
    const strats = w && w.strategies && typeof w.strategies === "object" ? w.strategies : null;
    const keys = strats ? Object.keys(strats) : [];
    if (keys.length === 0) {
      return escapeHtml(convertReasonUtcToTaipei(w.reason || ""));
    }
    return keys
      .map((sid) => {
        const entry = strats[sid] || {};
        const st = entry.status || "watch";
        const pillCls = st === "signal" ? "pill-ok" : (st === "expired" ? "pill-fail" : "pill-warn");
        const stZh = st === "signal" ? "已出單" : (st === "expired" ? "資料過期" : "觀望");
        const rsn = escapeHtml(convertReasonUtcToTaipei(entry.reason || ""));
        return `<div style="margin-bottom:3px;"><span class="pill ${pillCls}">${escapeHtml(sid)} ${stZh}</span> <span class="muted">${rsn}</span></div>`;
      })
      .join("");
  }

  function renderWatchToday(watchDataOverride) {
    const tbody = document.getElementById("watch-tbody");
    const sumBar = document.getElementById("watch-summary-bar");
    const moreWrap = document.getElementById("watch-more-wrap");
    const toggleBtn = document.getElementById("watch-toggle-btn");
    const scopeUpdEl = document.getElementById("watch-scope-updated");
    if (!tbody) return;
    const watchData =
      watchDataOverride || (currentWatchScope === "daily" ? cachedWatchDaily : cachedWatch);
    if (scopeUpdEl) {
      scopeUpdEl.textContent = watchData && watchData.updated_at_utc
        ? `更新時間 (台北)：${formatTaipeiTime(watchData.updated_at_utc)}`
        : (currentWatchScope === "daily" ? "尚無每日掃描紀錄（Daily Scan 後更新）" : "");
    }
    const rawItems = watchData && Array.isArray(watchData.items) ? watchData.items : [];
    const items = rawItems.filter((w) => w && w.market !== "forex");
    if (items.length === 0) {
      if (sumBar) sumBar.innerHTML = "";
      if (moreWrap) moreWrap.classList.add("hidden");
      tbody.innerHTML = currentWatchScope === "daily"
        ? '<tr><td colspan="5" class="muted center">尚無每日掃描紀錄（等待 Daily Scan 執行）</td></tr>'
        : '<tr><td colspan="5" class="muted center">尚無觀望紀錄（等待下次掃描）</td></tr>';
      return;
    }

    const enriched = items.map((w) => {
      const basis = w.decision === "signal" ? "已發布" : (w.reason || "");
      const cat = classifyWatchReason(basis);
      return { ...w, _cat: cat.label, _rank: cat.rank };
    });
    enriched.sort((a, b) => (a._rank - b._rank) || String(a.symbol).localeCompare(String(b.symbol)));

    const counts = {};
    const rankMap = {};
    enriched.forEach((w) => {
      counts[w._cat] = (counts[w._cat] || 0) + 1;
      rankMap[w._cat] = w._rank;
    });
    const sortedCats = Object.keys(counts).sort((a, b) => rankMap[a] - rankMap[b]);
    if (currentWatchCategory !== "all" && !counts[currentWatchCategory]) {
      currentWatchCategory = "all";
    }

    if (sumBar) {
      const chips = [`<button class="watch-chip ${currentWatchCategory === "all" ? "active" : ""}" data-cat="all" type="button">全部 ${enriched.length}</button>`];
      sortedCats.forEach((c) => {
        chips.push(`<button class="watch-chip ${currentWatchCategory === c ? "active" : ""}" data-cat="${escapeHtml(c)}" type="button">${escapeHtml(c)} ${counts[c]}</button>`);
      });
      sumBar.innerHTML = chips.join("");
    }

    const filtered = currentWatchCategory === "all" ? enriched : enriched.filter((w) => w._cat === currentWatchCategory);
    const limit = 15;
    const visible = showAllWatch ? filtered : filtered.slice(0, limit);

    if (moreWrap && toggleBtn) {
      if (filtered.length > limit) {
        moreWrap.classList.remove("hidden");
        toggleBtn.textContent = showAllWatch ? `收合為前 ${limit} 個` : `顯示全部 ${filtered.length} 個`;
      } else {
        moreWrap.classList.add("hidden");
      }
    }

    tbody.innerHTML = visible
      .map((w) => {
        const ind = w.indicators || {};
        const hasNewAtr = ind.atr_1h !== null && ind.atr_1h !== undefined;
        const hasOldInd =
          (ind.adx_1h !== null && ind.adx_1h !== undefined) ||
          (ind.rsi_14 !== null && ind.rsi_14 !== undefined) ||
          (ind.atr_ratio !== null && ind.atr_ratio !== undefined);
        let indTxt = '<span class="muted">此時段不計算</span>';
        if (hasNewAtr) {
          indTxt = `1H ATR ${formatNumber(ind.atr_1h) ?? "--"}${ind.atr_ratio !== null && ind.atr_ratio !== undefined ? ` / ${ind.atr_ratio}x` : ""}`;
        } else if (hasOldInd) {
          indTxt = `ADX ${ind.adx_1h ?? "--"} / RSI ${ind.rsi_14 ?? "--"} / ATR ${ind.atr_ratio ?? "--"}x`;
        } else if (w.data_status_1d) {
          indTxt = `日線：${escapeHtml(w.data_status_1d)}`;
        }
        let badge = '<span class="pill pill-warn">觀望</span>';
        if (w.decision === "signal") badge = '<span class="pill pill-ok">已出單</span>';
        else if (w.decision === "rejected") badge = '<span class="pill pill-fail">風控擋下</span>';
        else if (/資料過期/.test(w.reason || "")) badge = '<span class="pill pill-fail">資料過期</span>';
        const strategiesHtml = formatStrategyJudgments(w);
        return `
          <tr>
            <td data-label="標的"><strong>${escapeHtml(formatInstrumentTitle(w.symbol))}</strong></td>
            <td data-label="市場">${marketLabel(w.market, w.symbol)}</td>
            <td data-label="判定">${badge}</td>
            <td data-label="波動 / 狀態指標">${indTxt}</td>
            <td data-label="各策略目前判斷與原因">${strategiesHtml}</td>
          </tr>`;
      })
      .join("");
  }

  function renderProbe(probeData) {
    const summaryEl = document.getElementById("probe-summary");
    const badgeEl = document.getElementById("probe-date-badge");
    const tableEl = document.getElementById("probe-table");
    const tbody = document.getElementById("probe-tbody");
    if (!probeData || !Array.isArray(probeData.results) || probeData.results.length === 0) {
      if (badgeEl) badgeEl.textContent = "尚未實測";
      summaryEl.textContent = "尚無實測報告（手動執行才會更新，請於 GitHub Actions 手動觸發實測）。";
      tableEl.classList.add("hidden");
      return;
    }
    const probeRows = probeData.results.filter((r) => r && r.market !== "forex");
    const probeTime = formatTaipeiTime(probeData.generated_at_utc);
    if (badgeEl) badgeEl.textContent = probeTime;
    summaryEl.textContent = `實測時間 (台北)：${probeTime}，共 ${probeRows.length} 組測試（手動執行才會更新）。`;
    tableEl.classList.remove("hidden");
    tbody.innerHTML = probeRows
      .map((row) => {
        const attemptsHtml = (row.attempts || [])
          .map((a) => `<span class="pill ${a.ok ? "pill-ok" : "pill-fail"}" title="${a.error || ""}">${a.ok ? `${a.adapter}: OK (${a.bar_count}根, ${a.latency_ms}ms)` : `${a.adapter}: 失敗`}</span>`)
          .join(" ");
        const missPct = row.missing_ratio !== undefined ? `${(row.missing_ratio * 100).toFixed(1)}%` : "--";
        const delayTxt = row.delay_minutes !== undefined && row.delay_minutes !== null ? `${row.delay_minutes}m` : "--";
        const reasonTxt = row.quality_reason ? `<br><span class="muted">缺K:${missPct} 延遲:${delayTxt}｜${row.quality_reason}</span>` : "";
        return `<tr><td><strong>${row.symbol}</strong></td><td>${row.interval}</td><td>${statusPill(row.quality_status)}${reasonTxt}</td><td>${attemptsHtml || '<span class="muted">無</span>'}</td></tr>`;
      })
      .join("");
  }

  function renderRunLog(runLog, health) {
    const box = document.getElementById("run-log-box");
    const sumEl = document.getElementById("sched-summary");
    if (sumEl && health && health.runs_last_24h !== undefined) {
      sumEl.textContent = `近 24 小時排程執行次數：${health.runs_last_24h} 次｜自動排程時間：每小時台北時間 :07/:22/:37/:52 前後`;
    }
    if (!Array.isArray(runLog) || runLog.length === 0) {
      box.textContent = "尚無排程紀錄。";
      return;
    }
    const items = runLog.slice(0, 10).map((r) => {
      const cls = r.status === "ok" ? "pill-ok" : "pill-fail";
      const jobTag = r.job ? `[${escapeHtml(r.job)}] ` : "";
      const instTxt = r.instruments_ok !== undefined ? `${r.instruments_ok}/${r.instruments_total} 正常` : "完成";
      const sigInfo = r.signals_published !== undefined ? `｜報單 ${r.signals_published}、觀望 ${r.watch_count ?? 0}` : "";
      const tm = r.step_timings_sec;
      const timingTxt = tm
        ? `｜步驟：抓K ${tm.fetch_and_quality_sec ?? tm.fetch_and_daily_eval_sec ?? "--"}s / 回放 ${tm.indicators_and_replay_sec ?? 0}s / 衍生品 ${tm.derivatives_fetch_sec ?? 0}s / 策略 ${tm.strategy_eval_sec ?? tm.l3_rebalance_sec ?? 0}s / 存檔 ${tm.export_and_save_sec ?? 0}s`
        : "";
      return `<li>${formatTaipeiTime(r.run_at_utc)} — ${jobTag}<span class="pill ${cls}">${r.status}</span>（${instTxt}${sigInfo}，總耗時 ${r.duration_sec}s${timingTxt}）</li>`;
    });
    box.innerHTML = `<ul>${items.join("")}</ul>`;
  }

  function updateLights(health, control) {
    const riskLight = document.getElementById("risk-light");
    const controlBanner = document.getElementById("control-banner");
    if (control && control.paused) {
      riskLight.className = "light light-danger";
      riskLight.textContent = "風控：暫停報單";
      controlBanner.textContent = `⛔ 目前已暫停報單：${control.pause_reason || "手動暫停"}`;
      controlBanner.classList.remove("hidden");
    } else {
      riskLight.className = "light light-ok";
      riskLight.textContent = "風控：正常";
      controlBanner.classList.add("hidden");
    }

    const dataLight = document.getElementById("data-light");
    if (!health) {
      dataLight.className = "light light-danger";
      dataLight.textContent = "資料：資料來源失敗";
    } else if (health.overall === "ok") {
      dataLight.className = "light light-ok";
      dataLight.textContent = `資料：正常 (${health.status_counts.ok}/${health.total_instruments})`;
    } else if (health.overall === "failed") {
      dataLight.className = "light light-danger";
      dataLight.textContent = "資料：資料來源失敗";
    } else {
      dataLight.className = "light light-warn";
      dataLight.textContent = `資料：部分異常 (${health.status_counts.ok}/${health.total_instruments})`;
    }

    const schedLight = document.getElementById("sched-light");
    const lastUpdatedEl = document.getElementById("last-updated");
    const nextScanEl = document.getElementById("next-scan-est");
    const delayBanner = document.getElementById("delay-banner");

    if (nextScanEl) {
      const now = new Date();
      const utcMin = now.getUTCMinutes();
      const slots = [7, 22, 37, 52];
      const nextSlot = slots.find((m) => m > utcMin);
      const minsUntil = nextSlot !== undefined ? (nextSlot - utcMin) : (60 - utcMin + 7);
      nextScanEl.textContent = `（下次掃描約在 ${minsUntil} 分鐘後）`;
    }

    if (health && health.updated_at_utc) {
      lastUpdatedEl.textContent = formatTaipeiTime(health.updated_at_utc);
      const ageMin = (Date.now() - new Date(health.updated_at_utc).getTime()) / 60000;
      if (ageMin > 40) {
        schedLight.className = "light light-warn";
        schedLight.textContent = `排程：排程延遲 (${Math.round(ageMin)}m)`;
        delayBanner.classList.remove("hidden");
      } else {
        schedLight.className = "light light-ok";
        schedLight.textContent = "排程：正常";
        delayBanner.classList.add("hidden");
      }
    } else {
      lastUpdatedEl.textContent = "--";
      schedLight.className = "light light-danger";
      schedLight.textContent = "排程：無資料";
    }
  }

  function escapeHtml(str) {
    return String(str ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function formatMetricSummary(m) {
    if (!m || typeof m.trades !== "number") return "--";
    if (m.trades === 0) return "0 筆";
    const wr = Number(m.win_rate_pct || 0).toFixed(1);
    const exp = Number(m.expectancy_r || 0).toFixed(3);
    const pf = m.profit_factor !== null && m.profit_factor !== undefined ? Number(m.profit_factor).toFixed(2) : "--";
    const dd = Number(m.max_drawdown_pct || 0).toFixed(2);
    return `${m.trades} 筆｜勝率 ${wr}%｜期望 ${exp}R｜PF ${pf}｜回撤 ${dd}%`;
  }

  function renderBacktestResults() {
    const tbody = document.getElementById("backtest-tbody");
    const metaEl = document.getElementById("bt-summary-meta");
    if (!tbody || !metaEl) return;

    const list = cachedBacktest
      ? (Array.isArray(cachedBacktest.strategies) ? cachedBacktest.strategies : cachedBacktest.results)
      : null;

    if (!list || !Array.isArray(list) || list.length === 0) {
      metaEl.textContent = "目前尚無六個策略回測結果（等待執行 Weekly Backtest）。";
      tbody.innerHTML = '<tr><td colspan="8" class="muted center">目前尚無策略回測結果。</td></tr>';
      return;
    }

    const s = cachedBacktest.summary || {};
    metaEl.textContent = `最新回測 (台北)：${formatTaipeiTime(cachedBacktest.generated_at_utc)}｜共 ${s.total_strategies ?? list.length} 個策略｜通過 ${s.passed ?? 0} 個｜未通過 ${s.failed ?? 0} 個｜歷史不足 5 年 ${s.insufficient_history ?? 0} 個（歷史不代表未來）`;

    const orderedList = [
      ...list.filter((item) => item.strategy_id !== "S1"),
      ...list.filter((item) => item.strategy_id === "S1"),
    ];
    if (!orderedList.some((item) => item.strategy_id === "S1")) {
      orderedList.push({
        strategy_id: "S1",
        name: "開盤區間突破（已停用）",
        pool: "short_term",
        interval: "15m",
        capital_usd: 10000,
        passed: false,
        verification_label: "已停用",
        reason: "已停止開新倉，歷史紀錄保留供對照。",
      });
    }

    tbody.innerHTML = orderedList
      .map((item) => {
        const isRetiredS1 = item.strategy_id === "S1";
        const label = isRetiredS1
          ? "已停用"
          : (item.verification_label || (item.passed ? "已驗證" : (item.status === "insufficient_history" ? "資料不足，觀察中" : "觀察中（未通過驗證）")));
        const statusBadge = item.passed
          ? `<span class="pill pill-ok">${escapeHtml(label)}</span>`
          : `<span class="pill pill-warn">${escapeHtml(label)}</span>`;

        const oos = item.out_of_sample || {};
        const tradesTxt = typeof oos.trades === "number" ? `${oos.trades} 筆` : "--";
        const expVal = oos.expectancy_r !== undefined && oos.expectancy_r !== null ? `${Number(oos.expectancy_r).toFixed(3)}R` : "--";
        const pfVal = oos.profit_factor !== undefined && oos.profit_factor !== null ? Number(oos.profit_factor).toFixed(2) : "--";
        const ddVal = oos.max_drawdown_pct !== undefined && oos.max_drawdown_pct !== null ? `${Number(oos.max_drawdown_pct).toFixed(2)}%` : "--";
        const yrsNum = Number(item.history_years || 0);
        const yrsText = yrsNum > 0 ? `${yrsNum < 0.1 ? yrsNum.toFixed(4) : yrsNum.toFixed(2)} 年` : "--";
        const appTxt = isRetiredS1 ? "--" : `${item.eligible_instruments ?? 0} / ${item.applicable_instruments ?? 0} 檔（可用 ${yrsText}）`;
        const poolLabel = item.pool === "short_term" ? "短線" : "長線";
        const noteHtml = item.data_note ? `<div class="muted" style="margin-top:4px;font-size:0.82rem;">註：${escapeHtml(item.data_note)}</div>` : "";
        const retiredHtml = isRetiredS1
          ? `<div class="muted" style="margin-top:4px;font-size:0.78rem;white-space:normal;">${escapeHtml(S1_RETIRED_NOTE)}</div>`
          : "";
        const paramsText = item.chosen_params && Object.keys(item.chosen_params).length > 0
          ? Object.entries(item.chosen_params).map(([k, v]) => `${k}=${v}`).join(", ")
          : "--";
        const detailHtml = `
          <div>${escapeHtml(item.reason || "--")}</div>
          ${noteHtml}
          ${retiredHtml}
          <details class="bt-details">
            <summary>參數與樣本內詳細</summary>
            <div class="bt-details-body">
              <div>資金配置：${formatUsd(item.capital_usd)}（週期 ${escapeHtml(item.interval || "")}）</div>
              <div>樣本內選出參數：<code>${escapeHtml(paramsText)}</code></div>
              <div>樣本內 (IS)：${formatMetricSummary(item.in_sample)}</div>
            </div>
          </details>
        `;

        return `
          <tr>
            <td data-label="策略"><strong>${escapeHtml(item.strategy_id || item.symbol)}</strong> ${escapeHtml(item.name || "")}<br><span class="muted">${poolLabel}・${escapeHtml(item.interval || "")}</span>${retiredHtml}</td>
            <td data-label="通過狀態">${statusBadge}</td>
            <td data-label="樣本外交易數">${tradesTxt}</td>
            <td data-label="期望值">${expVal}</td>
            <td data-label="獲利因子 (PF)">${pfVal}</td>
            <td data-label="最大回撤">${ddVal}</td>
            <td data-label="適用標的數">${appTxt}</td>
            <td data-label="原因／詳細">${detailHtml}</td>
          </tr>
        `;
      })
      .join("");
  }

  function unverifiedCell() {
    return '<span class="pill pill-warn">尚未驗證</span>';
  }

  function renderPerformanceStats() {
    const tbody = document.getElementById("stats-tbody");
    const metaEl = document.getElementById("stats-meta");
    const noticeEl = document.getElementById("stats-notice");
    if (!tbody || !metaEl || !noticeEl) return;

    if (!cachedPerf || !cachedPerf.overall) {
      noticeEl.classList.add("hidden");
      metaEl.textContent = "目前尚無績效統計資料（等待排程產生 performance_stats.json）。";
      tbody.innerHTML = '<tr><td colspan="10" class="muted center">目前尚無績效統計資料。</td></tr>';
      return;
    }

    const minN = cachedPerf.min_verified_trades || 50;
    metaEl.textContent = `最後統計時間 (台北)：${formatTaipeiTime(cachedPerf.updated_at_utc)}｜最低驗證樣本門檻：${minN} 筆`;

    const s1Stats = cachedPerf.by_strategy?.S1 || {
      retired: true,
      verified: false,
      closed_trades: 0,
      min_trades_required: minN,
    };

    const rows = [
      ["總計 (Overall)", cachedPerf.overall, false],
      [STRATEGY_NAMES.S2, cachedPerf.by_strategy?.S2, false],
      [STRATEGY_NAMES.S3, cachedPerf.by_strategy?.S3, false],
      [STRATEGY_NAMES.L1, cachedPerf.by_strategy?.L1, false],
      [STRATEGY_NAMES.L2, cachedPerf.by_strategy?.L2, false],
      [STRATEGY_NAMES.L3, cachedPerf.by_strategy?.L3, false],
      [STRATEGY_NAMES.L4, cachedPerf.by_strategy?.L4, false],
      ["市場：加密貨幣", cachedPerf.by_market?.crypto, false],
      ["市場：台股", cachedPerf.by_market?.tw_stock, false],
      ["市場：美股／個股", cachedPerf.by_market?.us_stock, false],
      ["市場：商品", cachedPerf.by_market?.commodity, false],
      ["市場：指數", cachedPerf.by_market?.index, false],
      ["方向：做多", cachedPerf.by_direction?.long, false],
      ["方向：做空", cachedPerf.by_direction?.short, false],
      [STRATEGY_NAMES.S1, s1Stats, true],
    ].filter(([, m]) => Boolean(m));

    if (!cachedPerf.overall.verified) {
      noticeEl.textContent = `目前總已結束報單數為 ${cachedPerf.overall.closed_trades} 筆（少於 ${minN} 筆），樣本不足，一律標示「尚未驗證」且不顯示勝率與報酬率數字。`;
      noticeEl.classList.remove("hidden");
    } else {
      noticeEl.classList.add("hidden");
    }

    tbody.innerHTML = rows
      .map(([label, m, isRetiredS1]) => {
        const verBadge = m.verified ? '<span class="pill pill-ok">已驗證</span>' : '<span class="pill pill-warn">尚未驗證</span>';
        const cntText = `${m.closed_trades || 0} / ${m.min_trades_required || minN}`;
        const labelHtml = `<strong>${escapeHtml(label)}</strong>${
          isRetiredS1 ? `<div class="muted" style="font-size:0.78rem;margin-top:2px;white-space:normal;">${escapeHtml(S1_RETIRED_NOTE)}</div>` : ""
        }`;
        if (!m.verified) {
          return `
            <tr>
              <td>${labelHtml}</td>
              <td>${verBadge}</td>
              <td>${cntText}</td>
              <td>${unverifiedCell()}</td>
              <td>${unverifiedCell()}</td>
              <td>${unverifiedCell()}</td>
              <td>${unverifiedCell()}</td>
              <td>${unverifiedCell()}</td>
              <td>${unverifiedCell()}</td>
              <td>${unverifiedCell()}</td>
            </tr>
          `;
        }
        return `
          <tr>
            <td>${labelHtml}</td>
            <td>${verBadge}</td>
            <td>${cntText}</td>
            <td>${Number(m.win_rate_pct).toFixed(2)}%</td>
            <td>${Number(m.avg_r).toFixed(3)}R</td>
            <td>${Number(m.profit_factor).toFixed(2)}</td>
            <td>${Number(m.max_drawdown_pct).toFixed(2)}%</td>
            <td>${Number(m.sharpe).toFixed(2)} / ${Number(m.recovery_factor).toFixed(2)}</td>
            <td>${m.max_consecutive_losses ?? 0}</td>
            <td>${Number(m.capital_efficiency_pct).toFixed(2)}%</td>
          </tr>
        `;
      })
      .join("");
  }


  function renderOpenPositionsHtml(positionsList) {
    if (!Array.isArray(positionsList) || positionsList.length === 0) {
      return '<tr><td colspan="6" class="muted center">目前無持倉。</td></tr>';
    }
    return positionsList
      .map((p) => {
        const pxInfo = getDisplayPrice(p.symbol);
        const refPx = pxInfo.price !== null ? pxInfo.price : (p.mark_price || p.avg_entry_price || 0);
        const qty = Number(p.total_qty || p.remaining_qty || 0);
        const entryPx = Number(p.avg_entry_price || p.entry_price || 0);
        let floatUsd = p.unrealized_pnl_usd || 0;
        if (pxInfo.price !== null && qty > 0 && entryPx > 0) {
          floatUsd = p.direction === "long" ? (pxInfo.price - entryPx) * qty : (entryPx - pxInfo.price) * qty;
        }
        const est = computeCardEstimates(p);
        const enteredTp = formatTaipeiTime(p.entered_at_utc || p.published_at_utc);
        const ruleTxt = convertReasonUtcToTaipei(p.exit_rule || getStrategyExitRuleText(p) || "依策略規則出場");
        const curSl = p.current_stop_loss ?? p.initial_stop_loss ?? p.stop_loss;
        return `
          <tr>
            <td data-label="報單 / 標的"><a href="#/signal/${encodeURIComponent(p.signal_id)}">${escapeHtml(p.signal_id)}</a><br><strong>${escapeHtml(p.symbol)}</strong> (${escapeHtml(p.strategy)} / ${p.direction === "long" ? "多" : "空"})</td>
            <td data-label="進場時間 (台北) / 成本 / 數量">台北 ${enteredTp}<br><span class="muted">${entryPx > 0 ? formatNumber(entryPx) : "--"} / ${qty}</span></td>
            <td data-label="倉位名目 / 佔策略資金">$${fmtCompactUsd(est.notional)}<br><span class="muted">佔 ${escapeHtml(p.strategy)} ${est.notionalPct.toFixed(1)}%</span></td>
            <td data-label="即時現價 / 未實現損益">${formatNumber(refPx) || "--"} (${escapeHtml(pxInfo.source)})<br><span class="${floatUsd >= 0 ? "chg-up" : "chg-down"}">${formatUsd(floatUsd)}</span></td>
            <td data-label="止損 / 預估 SL 虧損">${formatNumber(curSl)}<br><span class="chg-down">-$${fmtCompactUsd(est.slUsd)} (${est.slPct.toFixed(1)}%)</span></td>
            <td data-label="出場規則">${escapeHtml(ruleTxt)}</td>
          </tr>
        `;
      })
      .join("");
  }


  function renderSimulatedAccount(riskObj, tradesList) {
    const risk = riskObj !== undefined ? riskObj : cachedRisk;
    const trades = tradesList !== undefined ? tradesList : cachedTrades;
    const acc = risk?.account;
    const elTot = document.getElementById("acc-total-eq");
    if (elTot) elTot.textContent = acc ? formatUsd(acc.equity_usd) : "--";
    const elShort = document.getElementById("acc-short-eq");
    if (elShort) elShort.textContent = acc ? formatUsd(acc.short_term_equity_usd) : "--";
    const elLong = document.getElementById("acc-long-eq");
    if (elLong) elLong.textContent = acc ? formatUsd(acc.long_term_equity_usd) : "--";
    const positions = (Array.isArray(risk?.open_positions) && risk.open_positions.length > 0)
      ? risk.open_positions
      : (Array.isArray(trades) ? trades.filter((t) => t && t.state === "active") : []);
    const elOpen = document.getElementById("acc-open-margin");
    if (elOpen) {
      elOpen.textContent = acc
        ? `${positions.length} 筆 / ${formatUsd(acc.total_open_notional_usd || 0)}`
        : "--";
    }

    const chartBox = document.getElementById("equity-chart-box");
    const chartNote = document.getElementById("equity-chart-note");
    if (chartBox && chartNote) {
      if (activeEquityChart) {
        try { activeEquityChart.remove(); } catch (_) {}
        activeEquityChart = null;
      }
      const pts = cachedEquity?.points || [];
      const isHiddenBox = chartBox.clientWidth === 0;
      if (pts.length === 0 || !window.LightweightCharts) {
        chartBox.innerHTML = "";
        chartNote.textContent = pts.length === 0 ? "目前尚無淨值快照資料（等待排程產生 equity_curve.json）。" : "圖表函式庫載入失敗。";
      } else if (isHiddenBox) {
        chartBox.innerHTML = "";
        chartNote.textContent = `共有 ${pts.length} 筆淨值快照。`;
      } else {
        chartBox.innerHTML = "";
        activeEquityChart = window.LightweightCharts.createChart(chartBox, {
          width: chartBox.clientWidth || 720,
          height: chartBox.clientHeight || 340,
          layout: { background: { color: "#0d1117" }, textColor: "#c9d1d9" },
          grid: { vertLines: { color: "#1f242c" }, horzLines: { color: "#1f242c" } },
          timeScale: { timeVisible: true, secondsVisible: false },
        });
        const totalSeries = activeEquityChart.addLineSeries({ color: "#58a6ff", lineWidth: 2, title: "總淨值" });
        const toChartData = (key) => {
          const seen = new Map();
          pts.forEach((p) => {
            const t = Math.floor(new Date(p.ts_utc).getTime() / 1000);
            const v = Number(p[key]);
            if (!Number.isNaN(t) && !Number.isNaN(v)) seen.set(t, v);
          });
          return Array.from(seen.entries()).sort((a, b) => a[0] - b[0]).map(([time, value]) => ({ time, value }));
        };
        totalSeries.setData(toChartData("total_equity_usd"));
        activeEquityChart.timeScale().fitContent();
        chartNote.textContent = `共有 ${pts.length} 筆淨值快照。`;
      }
    }

    const posTbody = document.getElementById("account-positions-tbody");
    if (!posTbody) return;
    if (!risk && (!trades || trades.length === 0)) {
      posTbody.innerHTML = '<tr><td colspan="6" class="muted center">目前尚無模擬持倉資料（等待排程產生 risk_status.json）。</td></tr>';
      return;
    }
    posTbody.innerHTML = renderOpenPositionsHtml(positions);
  }

  function renderMeter(title, currentLabel, limitLabel, ratioPct) {
    const pct = Math.max(0, Math.min(100, Number(ratioPct) || 0));
    const cls = pct >= 85 ? "danger" : (pct >= 60 ? "warn" : "");
    return `
      <div class="meter-card">
        <div class="meter-head"><strong>${escapeHtml(title)}</strong><span>${escapeHtml(currentLabel)} / ${escapeHtml(limitLabel)}</span></div>
        <div class="meter-bar"><div class="meter-fill ${cls}" style="width:${pct.toFixed(1)}%"></div></div>
      </div>
    `;
  }

  function renderRiskCenter() {
    const metaEl = document.getElementById("risk-meta");
    const metersGrid = document.getElementById("risk-meters-grid");
    const subTbody = document.getElementById("risk-subaccounts-tbody");
    const eventsBox = document.getElementById("risk-events-box");
    if (!metaEl || !metersGrid || !subTbody || !eventsBox) return;

    if (!cachedRisk || !cachedRisk.account) {
      metaEl.textContent = "目前尚無風控狀態資料（等待排程產生 risk_status.json）。";
      metersGrid.innerHTML = '<div class="empty-state">目前尚無策略子帳戶狀態資料。</div>';
      subTbody.innerHTML = '<tr><td colspan="6" class="muted center">目前尚無策略子帳戶狀態資料。</td></tr>';
      eventsBox.innerHTML = '<p class="muted">目前尚無風控事件。</p>';
      return;
    }

    const a = cachedRisk.account;
    metaEl.textContent = `最後檢核 (台北)：${formatTaipeiTime(cachedRisk.updated_at_utc)}｜總淨值：${formatUsd(a.equity_usd)}｜最高淨值：${formatUsd(a.peak_equity_usd)}｜回撤：${Number(a.drawdown_pct || 0).toFixed(2)}%`;

    const subs = Array.isArray(cachedRisk.subaccounts) ? cachedRisk.subaccounts : [];
    const totalMaxRisk = subs.reduce((acc, s) => acc + Number(s.max_open_risk_usd || 0), 0) || 10000;
    const totalMaxNotional = subs.reduce((acc, s) => acc + Number(s.max_total_notional_usd || 0), 0) || 500000;

    metersGrid.innerHTML = [
      renderMeter("全策略總開放停損風險", formatUsd(a.total_open_risk_usd || 0), formatUsd(totalMaxRisk), ((a.total_open_risk_usd || 0) / totalMaxRisk) * 100),
      renderMeter("全策略總開倉名目", formatUsd(a.total_open_notional_usd || 0), formatUsd(totalMaxNotional), ((a.total_open_notional_usd || 0) / totalMaxNotional) * 100),
      renderMeter("帳戶總回撤", `${Number(a.drawdown_pct || 0).toFixed(2)}%`, "25.0% (OOS 門檻)", (Number(a.drawdown_pct || 0) / 25.0) * 100),
    ].join("");

    subTbody.innerHTML = subs.length === 0
      ? '<tr><td colspan="6" class="muted center">無策略子帳戶資料。</td></tr>'
      : subs.map((s) => {
          const poolZh = s.pool === "short_term" ? "短線池" : "長線池";
          const riskPctRatio = (Number(s.open_risk_usd || 0) / Math.max(1, Number(s.max_open_risk_usd || 1))) * 100;
          const notPctRatio = (Number(s.open_notional_usd || 0) / Math.max(1, Number(s.max_total_notional_usd || 1))) * 100;
          return `
            <tr>
              <td data-label="策略子帳戶"><strong>${escapeHtml(s.strategy_id)}</strong> ${escapeHtml(s.name)}</td>
              <td data-label="資金池 / 週期">${poolZh} / ${escapeHtml(s.interval)}</td>
              <td data-label="目前淨值">${formatUsd(s.equity_usd)}<br><span class="muted">已實現 ${formatUsd(s.realized_pnl_usd)} / 未實現 ${formatUsd(s.unrealized_pnl_usd)}</span></td>
              <td data-label="持倉數">${s.open_positions_count} / ${s.max_concurrent_positions} 檔</td>
              <td data-label="開放停損風險">${formatUsd(s.open_risk_usd)} / ${formatUsd(s.max_open_risk_usd)}<br><span class="muted">${Number(s.open_risk_pct).toFixed(1)}% (上限 ${s.max_total_open_risk_pct}%｜使用 ${riskPctRatio.toFixed(0)}%)</span></td>
              <td data-label="總名目倉位">${formatUsd(s.open_notional_usd)} / ${formatUsd(s.max_total_notional_usd)}<br><span class="muted">${Number(s.open_notional_pct).toFixed(1)}% (上限 ${s.max_total_notional_multiple}x｜使用 ${notPctRatio.toFixed(0)}%)</span></td>
            </tr>
          `;
        }).join("");

    const revents = cachedRisk.recent_risk_events || [];
    eventsBox.innerHTML = revents.length === 0
      ? '<p class="muted">目前尚無風控事件。</p>'
      : `<ul class="timeline">${revents.map((ev) => `<li class="timeline-item"><strong>台北 ${formatTaipeiTime(ev.ts_utc)}</strong>｜<code>${escapeHtml(ev.event_type)}</code>｜${escapeHtml(convertReasonUtcToTaipei(ev.reason || ev.note || ""))}</li>`).join("")}</ul>`;
  }

  let cachedResearch = null;

  async function fetchResearch() {
    if (cachedResearch) return cachedResearch;
    try {
      const resp = await fetch(`data/research.json?t=${Date.now()}`);
      if (resp.ok) {
        cachedResearch = await resp.json();
        return cachedResearch;
      }
    } catch (_) {}
    try {
      cachedResearch = await fetchDataJson("research.json");
      return cachedResearch;
    } catch (err) {
      console.warn("Failed to load research json:", err);
      return null;
    }
  }

  function formatContestPfCell(pf) {
    if (pf === null || pf === undefined || isNaN(Number(pf))) return "--";
    const num = Number(pf);
    const cls = num < 1.0 ? "pf-val pf-fail" : (num >= 1.2 ? "pf-val pf-pass" : "pf-val");
    return `<span class="${cls}">${num.toFixed(3)}</span>`;
  }

  function formatContestExpR(r) {
    if (r === null || r === undefined || isNaN(Number(r))) return "--";
    const num = Number(r);
    const sign = num > 0 ? "+" : "";
    return `${sign}${num.toFixed(4)}R`;
  }

  function formatContestPct(p, withSign = false) {
    if (p === null || p === undefined || isNaN(Number(p))) return "--";
    const num = Number(p);
    const sign = withSign && num > 0 ? "+" : "";
    return `${sign}${num.toFixed(2)}%`;
  }

  function renderContestTableRows(strategies) {
    if (!Array.isArray(strategies) || strategies.length === 0) {
      return '<tr><td colspan="8" class="muted center">尚無回測資料</td></tr>';
    }
    return strategies.map((s) => `
      <tr>
        <td data-label="策略"><strong>${escapeHtml(s.title || s.name || s.id)}</strong></td>
        <td data-label="交易數">${s.trades ?? "--"}</td>
        <td data-label="勝率">${formatContestPct(s.win_rate_pct)}</td>
        <td data-label="期望值 R">${formatContestExpR(s.expectancy_r)}</td>
        <td data-label="獲利因子 (PF)">${formatContestPfCell(s.profit_factor)}</td>
        <td data-label="壓力測試 PF">${formatContestPfCell(s.stress_pf)}</td>
        <td data-label="零成本 PF">${formatContestPfCell(s.zero_cost_pf)}</td>
        <td data-label="是否通過">
          <span class="badge ${s.passed ? "badge-ok" : "badge-danger"}">${s.passed ? "✅ 通過" : "❌ 未通過"}</span>
        </td>
      </tr>
    `).join("");
  }

  function renderContestSimRows(rows) {
    if (!Array.isArray(rows) || rows.length === 0) {
      return '<tr><td colspan="5" class="muted center">尚無模擬資料</td></tr>';
    }
    return rows.map((r) => {
      const isBase = Boolean(r.is_baseline);
      const rowCls = isBase ? 'class="highlight-row"' : "";
      const medCls = r.median_return_pct < 0 ? 'class="text-danger"' : (r.median_return_pct > 0 ? 'class="text-success"' : "");
      const bustCls = r.bust_rate_pct >= 40 ? 'class="text-danger"' : "";
      return `
        <tr ${rowCls}>
          <td data-label="策略"><strong>${escapeHtml(r.strategy)}</strong> ${isBase ? '<span class="badge badge-warn">基準</span>' : ""}</td>
          <td data-label="週期"><code>${escapeHtml(r.timeframe)}</code></td>
          <td data-label="一週報酬中位數"><span ${medCls}>${formatContestPct(r.median_return_pct, true)}</span></td>
          <td data-label="前 10% 報酬 (P90)"><span class="text-success">${formatContestPct(r.p90_return_pct, true)}</span></td>
          <td data-label="出局率 (<$9,000)"><span ${bustCls}>${formatContestPct(r.bust_rate_pct)}</span></td>
        </tr>
      `;
    }).join("");
  }

  function scrollToResearchArticle(id) {
    expandedResearchIds.add(id);
    const body = document.getElementById(`body-${id}`);
    const btn = document.getElementById(`toggle-btn-${id}`);
    if (body && body.classList.contains("collapsed")) {
      body.classList.remove("collapsed");
      if (btn) btn.textContent = "收折";
    }
    const el = document.getElementById(`article-${id}`);
    if (el) {
      try {
        el.scrollIntoView({ behavior: "smooth", block: "start" });
      } catch (_) {}
      try {
        const topY = el.getBoundingClientRect().top + (window.pageYOffset || document.documentElement.scrollTop || 0) - 70;
        window.scrollTo(0, Math.max(0, topY));
      } catch (_) {}
    }
  }
  window.scrollToResearchArticle = scrollToResearchArticle;

  function toggleResearchArticle(id) {
    const body = document.getElementById(`body-${id}`);
    const btn = document.getElementById(`toggle-btn-${id}`);
    if (!body) return;
    const isCollapsed = body.classList.toggle("collapsed");
    if (isCollapsed) expandedResearchIds.delete(id);
    else expandedResearchIds.add(id);
    if (btn) btn.textContent = isCollapsed ? "展開閱讀" : "收折";
  }
  window.toggleResearchArticle = toggleResearchArticle;

  function renderLockboxTableRows(rows) {
    if (!Array.isArray(rows) || rows.length === 0) {
      return '<tr><td colspan="6" class="muted center">尚無鎖定段資料</td></tr>';
    }
    return rows.map((r) => {
      const isBase = Boolean(r.is_baseline);
      const rowCls = isBase ? 'class="highlight-row"' : "";
      return `
        <tr ${rowCls}>
          <td data-label="策略"><strong>${escapeHtml(r.name || r.id)}</strong> ${isBase ? '<span class="badge badge-warn">基準</span>' : ""}</td>
          <td data-label="期望值 R">${formatContestExpR(r.expectancy_r)}</td>
          <td data-label="獲利因子 (PF)">${formatContestPfCell(r.profit_factor)}</td>
          <td data-label="年化報酬 (CAGR)"><span class="${r.cagr_pct > 0 ? "text-success" : (r.cagr_pct < 0 ? "text-danger" : "")}">${formatContestPct(r.cagr_pct, true)}</span></td>
          <td data-label="最大回撤 (MaxDD)"><span class="text-danger">-${Math.abs(Number(r.max_drawdown_pct || 0)).toFixed(2)}%</span></td>
          <td data-label="收益回撤比 (MAR)">${r.mar_ratio !== undefined && r.mar_ratio !== null ? Number(r.mar_ratio).toFixed(2) : "--"}</td>
        </tr>
      `;
    }).join("");
  }

  function renderKrakenTableRows(rows) {
    if (!Array.isArray(rows) || rows.length === 0) {
      return '<tr><td colspan="5" class="muted center">尚無海妖考試模擬資料</td></tr>';
    }
    return rows.map((r) => {
      const isBase = Boolean(r.is_baseline);
      const rowCls = isBase ? 'class="highlight-row"' : "";
      const passCls = r.pass_rate_pct >= 80 ? "text-success" : (r.pass_rate_pct < 30 ? "text-danger" : "");
      const bustCls = r.bust_rate_pct >= 40 ? "text-danger" : (r.bust_rate_pct <= 10 ? "text-success" : "");
      const medDays = r.median_days !== null && r.median_days !== undefined ? `${Number(r.median_days).toFixed(1)} 天` : "--";
      return `
        <tr ${rowCls}>
          <td data-label="策略"><strong>${escapeHtml(r.name || r.id)}</strong> ${isBase ? '<span class="badge badge-warn">基準</span>' : ""}</td>
          <td data-label="通關率"><span class="${passCls}"><strong>${formatContestPct(r.pass_rate_pct)}</strong></span></td>
          <td data-label="爆倉出局率"><span class="${bustCls}">${formatContestPct(r.bust_rate_pct)}</span></td>
          <td data-label="中位通關天數">${medDays}</td>
          <td data-label="實戰特性備註" class="muted">${escapeHtml(r.notes || "")}</td>
        </tr>
      `;
    }).join("");
  }

  function renderComboArticleHtml(art) {
    if (!art) return "";
    const takeaway = escapeHtml(art.takeaway || "");
    const sec = art.sections || {};
    const anti = sec.anti_self_deception || {};
    const truly = sec.truly_useful || {};
    const appar = sec.apparent_improvement || {};
    const use = sec.proven_useless || {};
    const caut = sec.cautions || {};
    const tbls = art.tables || {};
    const lockboxRows = tbls.lockbox_comparison || [];
    const krakenRows = tbls.kraken_scenarios || [];

    return `
      <section class="card research-article" id="article-combo">
        <div class="section-header article-header" onclick="toggleResearchArticle('combo')">
          <div>
            <h2>第 2 篇：${escapeHtml(art.title)}</h2>
            <p class="subtitle">${escapeHtml(art.subtitle || "")}</p>
          </div>
          <div class="article-header-actions" onclick="event.stopPropagation()">
            <span class="badge badge-ok">${escapeHtml(art.badge || "研究完成")}</span>
            <button type="button" class="btn btn-sm article-toggle-btn" id="toggle-btn-combo" onclick="toggleResearchArticle('combo')">展開閱讀</button>
          </div>
        </div>

        <div class="article-body collapsed" id="body-combo">
          <div class="callout callout-takeaway">
            <p>💡 <strong>核心結論：</strong>${takeaway}</p>
          </div>

          <div class="research-section">
            <h2>${escapeHtml(anti.title || "一、怎麼防止自欺：三段切分、61 組參數與統計檢定")}</h2>
            ${(anti.content || []).map((p) => `<p>${escapeHtml(p)}</p>`).join("")}
          </div>

          <div class="research-section">
            <h2>${escapeHtml(truly.title || "二、真的有用：5a 多策略組合把回撤砍半，碰巧機率僅 1%")}</h2>
            ${(truly.content || []).map((p) => `<p>${escapeHtml(p)}</p>`).join("")}
            ${truly.takeaway_pill ? `<div class="takeaway-pill">🎯 <strong>核心指引</strong>：${escapeHtml(truly.takeaway_pill)}</div>` : ""}
          </div>

          <div class="research-section">
            <h2>${escapeHtml(appar.title || "三、看起來有進步但要打折：策略合議實質加槓桿，週線過濾犧牲抗震力")}</h2>
            ${(appar.content || []).map((p) => `<p>${escapeHtml(p)}</p>`).join("")}
          </div>

          <div class="research-section">
            <h2>${escapeHtml(use.title || "四、實測無效的迷思：等回檔漏大魚、平半倉腰斬利潤、S2 正式退休")}</h2>
            ${(use.content || []).map((p) => `<p>${escapeHtml(p)}</p>`).join("")}
          </div>

          <div class="research-section">
            <h2>${escapeHtml(caut.title || "五、注意事項：鎖定段大牛市背景，切勿把順風當常態")}</h2>
            ${(caut.content || []).map((p) => `<p>${escapeHtml(p)}</p>`).join("")}
          </div>

          <div class="research-section">
            <h2>六、實測數據總表（鎖定段盲測與海妖考試模擬）</h2>
            <h3 style="margin: 16px 0 8px;">1. 鎖定段（2025-05 至 2026-11 盲測）候選策略 vs 原版基準</h3>
            <div class="table-wrap table-responsive">
              <table class="data-table mobile-cards">
                <thead>
                  <tr>
                    <th>策略</th>
                    <th>期望值 (Expectancy R)</th>
                    <th>獲利因子 (PF)</th>
                    <th>年化報酬 (CAGR)</th>
                    <th>最大回撤 (MaxDD)</th>
                    <th>收益回撤比 (MAR)</th>
                  </tr>
                </thead>
                <tbody>
                  ${renderLockboxTableRows(lockboxRows)}
                </tbody>
              </table>
            </div>

            <h3 style="margin: 24px 0 8px;">2. 海妖考試情境模擬（單槽持倉限制、10% 目標、5% 回撤淘汰）</h3>
            <div class="table-wrap table-responsive">
              <table class="data-table mobile-cards">
                <thead>
                  <tr>
                    <th>策略</th>
                    <th>通關率</th>
                    <th>爆倉出局率</th>
                    <th>中位通關天數</th>
                    <th>實戰特性備註</th>
                  </tr>
                </thead>
                <tbody>
                  ${renderKrakenTableRows(krakenRows)}
                </tbody>
              </table>
            </div>
          </div>

          <div class="research-section disclaimer-box">
            <p>⚠️ <strong>免責聲明</strong>：研究結果僅供參考，過去表現不代表未來，不構成投資建議。</p>
            <div class="github-links">
              <a href="${escapeHtml(art.github_report || 'https://github.com/diabetepanda/pandatesttrading/blob/main/research/combo/report.md')}" target="_blank" rel="noopener noreferrer" class="ext-link">🔗 完整組合研究報告 (report.md)</a>
              <a href="${escapeHtml(art.github_code || 'https://github.com/diabetepanda/pandatesttrading/tree/main/research/combo')}" target="_blank" rel="noopener noreferrer" class="ext-link">🔗 組合研究程式碼 (research/combo)</a>
            </div>
          </div>
        </div>
      </section>
    `;
  }

  function renderBestCombosTableRows(rows) {
    if (!Array.isArray(rows) || rows.length === 0) {
      return '<tr><td colspan="9" class="muted center">尚無各回撤底線最佳配置資料</td></tr>';
    }
    return rows.map((r) => {
      const is10 = r.dd_floor_pct === 10.0;
      const rowCls = is10 ? 'class="highlight-row"' : "";
      const passCls = r.pass_rate_pct >= 95 ? "text-success" : (r.pass_rate_pct >= 80 ? "" : "text-danger");
      const bustCls = r.bust_rate_pct === 0 ? "text-success" : (r.bust_rate_pct <= 5 ? "" : "text-danger");
      const medDays = r.median_days !== null && r.median_days !== undefined ? `${Number(r.median_days).toFixed(1)} 天` : "--";
      const prePass = r.pre_lockbox_pass_rate_pct !== null && r.pre_lockbox_pass_rate_pct !== undefined ? `${Number(r.pre_lockbox_pass_rate_pct).toFixed(1)}%` : "--";

      return `
        <tr ${rowCls}>
          <td data-label="回撤底線"><strong>${escapeHtml(r.label || `${r.dd_floor_pct}%`)}</strong></td>
          <td data-label="最佳策略"><strong>${escapeHtml(r.strategy)}</strong></td>
          <td data-label="每單風險">${r.risk_pct}%</td>
          <td data-label="持倉限制">${escapeHtml(r.position_limit || "1")}</td>
          <td data-label="過關率 (全期間)"><span class="${passCls}"><strong>${Number(r.pass_rate_pct).toFixed(2)}%</strong></span></td>
          <td data-label="爆倉率 (全期間)"><span class="${bustCls}">${Number(r.bust_rate_pct).toFixed(2)}%</span></td>
          <td data-label="中位通關天數">${medDays}</td>
          <td data-label="去牛市過關率"><span class="text-success">${prePass}</span></td>
          <td data-label="白話點評" class="muted">${escapeHtml(r.notes || "")}</td>
        </tr>
      `;
    }).join("");
  }

  function renderSensitivityMatrixTableRows(rows) {
    if (!Array.isArray(rows) || rows.length === 0) {
      return '<tr><td colspan="6" class="muted center">尚無矩陣資料</td></tr>';
    }
    const riskLevels = ["0.5", "0.75", "1.0", "1.5", "2.0"];
    return rows.map((r) => {
      const sname = escapeHtml(r.strategy_name || r.strategy_id);
      const isL2 = r.strategy_id === "L2";
      const cellsHtml = riskLevels.map((rl) => {
        const c = (r.cells && r.cells[rl]) || {};
        const p = Number(c.pass_rate_pct || 0);
        const b = Number(c.bust_rate_pct || 0);
        const d = c.median_days !== null && c.median_days !== undefined ? `${Number(c.median_days).toFixed(0)}天` : "--";

        let cellCls = "matrix-cell";
        if (p >= 90 && b <= 2) {
          cellCls += " pass-high";
        } else if (b >= 20 || p < 30) {
          cellCls += " bust-high";
        }

        const passColor = p >= 90 ? "text-success" : (p < 30 ? "text-danger" : "");
        const bustColor = b >= 15 ? "text-danger" : (b === 0 ? "text-success" : "");

        return `
          <td class="${cellCls}" data-label="風險 ${rl}%">
            <span class="matrix-pass ${passColor}"><strong>${p.toFixed(1)}%</strong></span>
            <span class="matrix-bust ${bustColor}">爆 ${b.toFixed(1)}%</span>
            <span class="matrix-days">${d}</span>
          </td>
        `;
      }).join("");

      return `
        <tr ${isL2 ? 'class="highlight-row"' : ""}>
          <td data-label="策略"><strong>${sname}</strong></td>
          ${cellsHtml}
        </tr>
      `;
    }).join("");
  }

  function renderExamSensitivityArticleHtml(art) {
    if (!art) return "";
    const takeaway = escapeHtml(art.takeaway || "");
    const sec = art.sections || {};
    const what = sec.what_we_simulated || {};
    const best = sec.best_combos || {};
    const matrix = sec.matrix_10pct || {};
    const findings = sec.three_findings || {};
    const findList = Array.isArray(findings.findings) ? findings.findings : [];
    const caveats = sec.caveats || {};
    const advice = sec.advice || {};
    const advList = Array.isArray(advice.points) ? advice.points : [];
    const tbls = art.tables || {};
    const bestRows = tbls.best_combos || [];
    const singleRows = tbls.single_slot_matrix || [];
    const multiRows = tbls.multi_slot_matrix || [];
    const disc = art.disclaimer || {};
    const ghLinks = Array.isArray(disc.github_links) ? disc.github_links : [];

    return `
      <section class="card research-article" id="article-exam_sensitivity">
        <div class="section-header article-header" onclick="toggleResearchArticle('exam_sensitivity')">
          <div>
            <h2>第 3 篇：${escapeHtml(art.title)}</h2>
            <p class="subtitle">${escapeHtml(art.subtitle || "")}</p>
          </div>
          <div class="article-header-actions" onclick="event.stopPropagation()">
            <span class="badge badge-ok">${escapeHtml(art.badge || "研究完成")}</span>
            <button type="button" class="btn btn-sm article-toggle-btn" id="toggle-btn-exam_sensitivity" onclick="toggleResearchArticle('exam_sensitivity')">展開閱讀</button>
          </div>
        </div>

        <div class="article-body collapsed" id="body-exam_sensitivity">
          <div class="callout callout-takeaway">
            <p>💡 <strong>核心結論：</strong>${takeaway}</p>
          </div>

          <div class="research-section">
            <h2>${escapeHtml(what.title || "一、我們模擬了什麼？（白話名詞與研究架構）")}</h2>
            ${(what.content || []).map((p) => `<p>${escapeHtml(p)}</p>`).join("")}
          </div>

          <div class="research-section">
            <h2>${escapeHtml(best.title || "二、主要結果表：各回撤底線下的最佳配置")}</h2>
            ${(best.content || []).map((p) => `<p>${escapeHtml(p)}</p>`).join("")}
            <div class="table-wrap table-responsive" style="margin-top: 14px;">
              <table class="data-table mobile-cards">
                <thead>
                  <tr>
                    <th>回撤底線</th>
                    <th>最佳策略</th>
                    <th>每單風險</th>
                    <th>持倉限制</th>
                    <th>過關率 (全期間)</th>
                    <th>爆倉率 (全期間)</th>
                    <th>中位通關天數</th>
                    <th>去牛市過關率</th>
                    <th>白話點評</th>
                  </tr>
                </thead>
                <tbody>
                  ${renderBestCombosTableRows(bestRows)}
                </tbody>
              </table>
            </div>
          </div>

          <div class="research-section">
            <h2>${escapeHtml(matrix.title || "三、10% 回撤策略矩陣：策略 × 每單風險（一次一單 vs 最多 3 單）")}</h2>
            ${(matrix.content || []).map((p) => `<p>${escapeHtml(p)}</p>`).join("")}

            <h3 style="margin: 20px 0 8px;">1. 一次一單（Single Slot）：過關率／爆倉率／中位天數</h3>
            <p class="muted" style="margin-bottom: 8px;">綠色底代表過關率 ≥ 90% 且極低爆倉；紅色底代表爆倉風險高或過關率過低。</p>
            <div class="table-wrap table-responsive">
              <table class="data-table mobile-cards">
                <thead>
                  <tr>
                    <th>策略名稱</th>
                    <th>風險 0.5%</th>
                    <th>風險 0.75%</th>
                    <th>風險 1.0%</th>
                    <th>風險 1.5%</th>
                    <th>風險 2.0%</th>
                  </tr>
                </thead>
                <tbody>
                  ${renderSensitivityMatrixTableRows(singleRows)}
                </tbody>
              </table>
            </div>

            <h3 style="margin: 28px 0 8px;">2. 最多 3 單（Max 3 Positions）：過關率／爆倉率／中位天數</h3>
            <p class="muted" style="margin-bottom: 8px;">放寬持倉上限雖能縮短天數，但當各品種同步突破時，同向洗盤會大幅拉高爆倉率。</p>
            <div class="table-wrap table-responsive">
              <table class="data-table mobile-cards">
                <thead>
                  <tr>
                    <th>策略名稱</th>
                    <th>風險 0.5%</th>
                    <th>風險 0.75%</th>
                    <th>風險 1.0%</th>
                    <th>風險 1.5%</th>
                    <th>風險 2.0%</th>
                  </tr>
                </thead>
                <tbody>
                  ${renderSensitivityMatrixTableRows(multiRows)}
                </tbody>
              </table>
            </div>
          </div>

          <div class="research-section">
            <h2>${escapeHtml(findings.title || "四、三個重點發現")}</h2>
            ${findList.map((f) => `
              <div style="margin-bottom: 16px;">
                <h3 style="margin: 8px 0 4px; color: var(--accent);">${escapeHtml(f.title)}</h3>
                <p>${escapeHtml(f.text)}</p>
              </div>
            `).join("")}
          </div>

          <div class="research-section">
            <h2>${escapeHtml(caveats.title || "五、要打折看的地方：歷史起點挪動與大趨勢段檢驗")}</h2>
            ${(caveats.content || []).map((p) => `<p>${escapeHtml(p)}</p>`).join("")}
          </div>

          <div class="research-section">
            <h2>${escapeHtml(advice.title || "六、給考生的實戰操作建議（Cheat Sheet）")}</h2>
            <ul>
              ${advList.map((pt) => `<li>${escapeHtml(pt)}</li>`).join("")}
            </ul>
          </div>

          <div class="research-section disclaimer-box">
            <p>⚠️ <strong>免責聲明</strong>：${escapeHtml(disc.text || "研究結果僅供參考，過去表現不代表未來，不構成投資建議。")}</p>
            <div class="github-links">
              ${ghLinks.map((g) => `
                <a href="${escapeHtml(g.url)}" target="_blank" rel="noopener noreferrer" class="ext-link">🔗 ${escapeHtml(g.title)}</a>
              `).join("")}
            </div>
          </div>
        </div>
      </section>
    `;
  }

  function renderFlowSignalsArticleHtml(art) {
    if (!art) return "";
    const takeaway = escapeHtml(art.takeaway || "");
    const sec = art.sections || {};
    const tested = sec.what_we_tested || {};
    const periods = sec.data_periods || {};
    const eff = sec.effective_two || {};
    const useless = sec.useless_summary || {};
    const uselessItems = Array.isArray(useless.items) ? useless.items : [];
    const refOnly = sec.reference_only || {};
    const refItems = Array.isArray(refOnly.items) ? refOnly.items : [];
    const caution = sec.strict_caution || {};
    const effRows = (art.tables && art.tables.effective_indicators) || [];
    const disc = art.disclaimer || {};
    const ghLinks = Array.isArray(disc.github_links) ? disc.github_links : [];

    const effTableRows = effRows.length
      ? effRows
          .map(
            (r) => `
          <tr class="highlight-row">
            <td data-label="指標"><strong>${escapeHtml(r.name)}</strong><br><span class="muted">${escapeHtml(r.market || "")}</span></td>
            <td data-label="有效天期">${escapeHtml(r.horizon || "")}</td>
            <td data-label="觸發條件"><code>${escapeHtml(r.trigger || "")}</code></td>
            <td data-label="樣本外實證數字"><strong>${escapeHtml(r.oos_stat || "")}</strong></td>
            <td data-label="p / BH q 值">${escapeHtml(r.p_and_q || "")}</td>
            <td data-label="定位備註" class="muted">${escapeHtml(r.note || "")}</td>
          </tr>`
          )
          .join("")
      : '<tr><td colspan="6" class="muted center">尚無有效指標資料</td></tr>';

    return `
      <section class="card research-article" id="article-flow_signals">
        <div class="section-header article-header" onclick="toggleResearchArticle('flow_signals')">
          <div>
            <h2>第 4 篇：${escapeHtml(art.title || "大戶／法人資金流回測（2026-11）")}</h2>
            <p class="subtitle">${escapeHtml(art.subtitle || "")}</p>
          </div>
          <div class="article-header-actions" onclick="event.stopPropagation()">
            <span class="badge badge-warn">${escapeHtml(art.badge || "僅 2 項隔天提示")}</span>
            <button type="button" class="btn btn-sm article-toggle-btn" id="toggle-btn-flow_signals" onclick="toggleResearchArticle('flow_signals')">展開閱讀</button>
          </div>
        </div>

        <div class="article-body collapsed" id="body-flow_signals">
          <div class="callout callout-takeaway">
            <p>💡 <strong>核心結論：</strong>${takeaway}</p>
          </div>

          <div class="research-section">
            <h2>${escapeHtml(tested.title || "一、測了什麼？")}</h2>
            ${(tested.content || []).map((p) => `<p>${escapeHtml(p)}</p>`).join("")}
          </div>

          <div class="research-section">
            <h2>${escapeHtml(periods.title || "二、資料期間與限制")}</h2>
            ${(periods.content || []).map((p) => `<p>${escapeHtml(p)}</p>`).join("")}
          </div>

          <div class="research-section">
            <h2>${escapeHtml(eff.title || "三、唯二有效的台股隔天指標（5 日以上全面失效）")}</h2>
            ${(eff.content || []).map((p) => `<p>${escapeHtml(p)}</p>`).join("")}
            <div class="table-wrap table-responsive" style="margin-top: 12px;">
              <table class="data-table mobile-cards flow-eff-table">
                <thead>
                  <tr>
                    <th>指標 / 市場</th>
                    <th>有效天期</th>
                    <th>觸發條件</th>
                    <th>樣本外實證數字</th>
                    <th>統計檢定與 BH q 值</th>
                    <th>實戰定位</th>
                  </tr>
                </thead>
                <tbody>
                  ${effTableRows}
                </tbody>
              </table>
            </div>
          </div>

          <div class="research-section">
            <h2>${escapeHtml(useless.title || "四、哪些指標沒用？（48 項）")}</h2>
            <ul class="research-strat-list">
              ${uselessItems.map((it) => `<li><strong>${escapeHtml(it.group)}</strong>：${escapeHtml(it.detail)}</li>`).join("")}
            </ul>
          </div>

          <div class="research-section">
            <h2>${escapeHtml(refOnly.title || "五、哪些指標「僅供參考」？（7 項）")}</h2>
            <ul>
              ${refItems.map((pt) => `<li>${escapeHtml(pt)}</li>`).join("")}
            </ul>
          </div>

          <div class="research-section">
            <h2>${escapeHtml(caution.title || "六、重要提醒：嚴格統計標準下不顯著，只當隔天風險提示")}</h2>
            ${(caution.content || []).map((p) => `<p>${escapeHtml(p)}</p>`).join("")}
          </div>

          <div class="research-section disclaimer-box">
            <p>⚠️ <strong>免責聲明</strong>：${escapeHtml(disc.text || "嚴格統計標準下不顯著，只當隔天風險提示，不當進出場訊號。")}</p>
            <div class="github-links">
              ${ghLinks.map((g) => `<a href="${escapeHtml(g.url)}" target="_blank" rel="noopener noreferrer" class="ext-link">🔗 ${escapeHtml(g.title)}</a>`).join("")}
            </div>
          </div>
        </div>
      </section>
    `;
  }

  function renderDcaCellHtml(c) {
    if (!c) return '<div class="dca-cell muted">--</div>';
    const cagr = Number(c.cagr || 0);
    const maxdd = Number(c.maxdd || 0);
    const mar = c.mar !== null && c.mar !== undefined ? Number(c.mar).toFixed(2) : "--";
    const cagrCls = cagr > 0 ? "text-success" : (cagr < 0 ? "text-danger" : "");
    const cagrSign = cagr > 0 ? "+" : "";
    return `
      <div class="dca-cell">
        <div class="dca-cell-ret ${cagrCls}">${cagrSign}${cagr.toFixed(2)}%</div>
        <div class="dca-cell-dd">-${Math.abs(maxdd).toFixed(2)}%</div>
        <div class="dca-cell-mar">MAR ${mar}</div>
      </div>
    `;
  }

  function renderDcaCoreTableRows(rows) {
    if (!Array.isArray(rows) || rows.length === 0) {
      return '<tr><td colspan="7" class="muted center">尚無核心比較資料</td></tr>';
    }
    return rows.map((r) => {
      const isHybrid = String(r.plan_key || "").startsWith("Hybrid");
      const isLump = r.plan_key === "Lump_Sum";
      const isDca = r.plan_key === "DCA_Monthly";
      const highlightCls = (isHybrid || isLump || isDca) ? 'class="highlight-row"' : "";
      return `
        <tr ${highlightCls}>
          <td data-label="方案名稱">
            <strong>${escapeHtml(r.plan_name || r.plan_key)}</strong>
            <br><span class="badge ${isHybrid ? "badge-ok" : "badge-warn"}">${escapeHtml(r.category || "")}</span>
          </td>
          <td data-label="台股 ^TWII">${renderDcaCellHtml(r.twii)}</td>
          <td data-label="S&P 500">${renderDcaCellHtml(r.sp500)}</td>
          <td data-label="Nasdaq 100">${renderDcaCellHtml(r.nas100)}</td>
          <td data-label="黃金 XAUUSD">${renderDcaCellHtml(r.gold)}</td>
          <td data-label="比特幣 (7.1年)">${renderDcaCellHtml(r.btc)}</td>
          <td data-label="五標的組合">${renderDcaCellHtml(r.portfolio)}</td>
        </tr>
      `;
    }).join("");
  }

  function renderDcaRollingTableRows(rows) {
    if (!Array.isArray(rows) || rows.length === 0) {
      return '<tr><td colspan="6" class="muted center">尚無滾動測試資料</td></tr>';
    }
    let lastSym = "";
    return rows.map((r) => {
      const isNewSym = r.symbol !== lastSym;
      lastSym = r.symbol;
      const r3Med = Number(r.r3_median_cagr || 0);
      const r3Worst = Number(r.r3_worst_cagr || 0);
      const r5Med = Number(r.r5_median_cagr || 0);
      const r5Worst = Number(r.r5_worst_cagr || 0);
      const r3Cls = r3Med > 0 ? "text-success" : (r3Med < 0 ? "text-danger" : "");
      const r5Cls = r5Med > 0 ? "text-success" : (r5Med < 0 ? "text-danger" : "");
      const r5WinCls = r.r5_win_rate >= 100 ? "text-success font-bold" : "";
      return `
        <tr ${isNewSym ? 'class="group-start-row"' : ""}>
          <td data-label="標的 / 方案">
            <strong>${escapeHtml(r.symbol_name || r.symbol)}</strong> · ${escapeHtml(r.plan_name || r.plan_key)}
          </td>
          <td data-label="3 年中位數">
            <span class="${r3Cls}">${r3Med > 0 ? "+" : ""}${r3Med.toFixed(2)}%</span>
            <span class="muted text-xs"> / -${Math.abs(Number(r.r3_median_maxdd || 0)).toFixed(1)}%</span>
          </td>
          <td data-label="3 年最差情況">
            <span class="${r3Worst < 0 ? "text-danger" : ""}">${r3Worst > 0 ? "+" : ""}${r3Worst.toFixed(2)}%</span>
            <span class="muted text-xs"> / -${Math.abs(Number(r.r3_worst_maxdd || 0)).toFixed(1)}%</span>
          </td>
          <td data-label="5 年中位數">
            <span class="${r5Cls}">${r5Med > 0 ? "+" : ""}${r5Med.toFixed(2)}%</span>
            <span class="muted text-xs"> / -${Math.abs(Number(r.r5_median_maxdd || 0)).toFixed(1)}%</span>
          </td>
          <td data-label="5 年最差情況">
            <span class="${r5Worst < 0 ? "text-danger" : "text-success"}">${r5Worst > 0 ? "+" : ""}${r5Worst.toFixed(2)}%</span>
            <span class="muted text-xs"> / -${Math.abs(Number(r.r5_worst_maxdd || 0)).toFixed(1)}%</span>
          </td>
          <td data-label="5 年勝率">
            <span class="${r5WinCls}">${Number(r.r5_win_rate || 0).toFixed(1)}%</span>
          </td>
        </tr>
      `;
    }).join("");
  }

  function renderDcaCompareArticleHtml(art) {
    if (!art) return "";
    const takeaway = escapeHtml(art.takeaway || "");
    const sec = art.sections || {};
    const why = sec.why_low_return || {};
    const core = sec.core_table || {};
    const rolling = sec.rolling_summary || {};
    const practical = sec.practical_findings || {};
    const practicalItems = Array.isArray(practical.items) ? practical.items : [];
    const limitations = sec.limitations || {};
    const disc = art.disclaimer || {};
    const ghLinks = Array.isArray(disc.github_links) ? disc.github_links : [];

    const tables = art.tables || {};
    const coreRows = renderDcaCoreTableRows(tables.core_summary);
    const allRows = renderDcaCoreTableRows(tables.all_schemes);
    const rollingRows = renderDcaRollingTableRows(tables.rolling_summary);

    return `
      <section class="card research-article" id="article-dca_compare">
        <div class="section-header article-header" onclick="toggleResearchArticle('dca_compare')">
          <div>
            <h2>第 5 篇：${escapeHtml(art.title || "定期定額 vs 單筆買進 vs 六種策略（10 年實證）")}</h2>
            <p class="subtitle">${escapeHtml(art.subtitle || "")}</p>
          </div>
          <div class="article-header-actions" onclick="event.stopPropagation()">
            <span class="badge badge-ok">${escapeHtml(art.badge || "10 年多標的實證")}</span>
            <button type="button" class="btn btn-sm article-toggle-btn" id="toggle-btn-dca_compare" onclick="toggleResearchArticle('dca_compare')">展開閱讀</button>
          </div>
        </div>

        <div class="article-body collapsed" id="body-dca_compare">
          <div class="callout callout-takeaway">
            <p>💡 <strong>一句話結論：</strong>只看報酬，定期定額與單筆買進抱著遠勝六種策略；看回撤，策略穩很多。</p>
          </div>

          <div class="research-section">
            <h2>${escapeHtml(why.title || "二、為什麼策略報酬低？")}</h2>
            ${(why.content || []).map((p) => `<p>${escapeHtml(p)}</p>`).join("")}
          </div>

          <div class="research-section">
            <h2>${escapeHtml(core.title || "三、核心總表：五大標的與五標的組合全期間對決")}</h2>
            ${(core.content || []).map((p) => `<p class="muted">${escapeHtml(p)}</p>`).join("")}
            <div class="table-wrap table-responsive" style="margin-top: 12px;">
              <table class="data-table mobile-cards dca-table">
                <thead>
                  <tr>
                    <th>方案名稱 / 類別</th>
                    <th>台股 ^TWII<br><span class="muted text-xs">年化 / 回撤 / MAR</span></th>
                    <th>S&P 500<br><span class="muted text-xs">年化 / 回撤 / MAR</span></th>
                    <th>Nasdaq 100<br><span class="muted text-xs">年化 / 回撤 / MAR</span></th>
                    <th>黃金 XAUUSD<br><span class="muted text-xs">年化 / 回撤 / MAR</span></th>
                    <th>比特幣 BTC (7.1年)<br><span class="muted text-xs">年化 / 回撤 / MAR</span></th>
                    <th>五標的組合<br><span class="muted text-xs">年化 / 回撤 / MAR</span></th>
                  </tr>
                </thead>
                <tbody>
                  ${coreRows}
                </tbody>
              </table>
            </div>

            <details class="research-details" style="margin-top: 16px;">
              <summary class="cursor-pointer" style="font-weight: 600; color: var(--accent); padding: 8px 0;">展開完整 13 方案比較表（含 L1、L3、S3、S2、F4、每週定額、50/50 L2）▼</summary>
              <div class="table-wrap table-responsive" style="margin-top: 10px;">
                <table class="data-table mobile-cards dca-table">
                  <thead>
                    <tr>
                      <th>方案名稱 / 類別</th>
                      <th>台股 ^TWII</th>
                      <th>S&P 500</th>
                      <th>Nasdaq 100</th>
                      <th>黃金 XAUUSD</th>
                      <th>比特幣 BTC</th>
                      <th>五標的組合</th>
                    </tr>
                  </thead>
                  <tbody>
                    ${allRows}
                  </tbody>
                </table>
              </div>
            </details>
          </div>

          <div class="research-section">
            <h2>${escapeHtml(rolling.title || "四、滾動測試重點（持有 3 年與 5 年中位數與最差情況）")}</h2>
            ${(rolling.content || []).map((p) => `<p>${escapeHtml(p)}</p>`).join("")}
            <div class="table-wrap table-responsive" style="margin-top: 12px;">
              <table class="data-table mobile-cards dca-rolling-table">
                <thead>
                  <tr>
                    <th>標的 / 方案</th>
                    <th>3 年中位數<br><span class="muted text-xs">年化 / 回撤</span></th>
                    <th>3 年最差情況<br><span class="muted text-xs">年化 / 回撤</span></th>
                    <th>5 年中位數<br><span class="muted text-xs">年化 / 回撤</span></th>
                    <th>5 年最差情況<br><span class="muted text-xs">年化 / 回撤</span></th>
                    <th>5 年勝率</th>
                  </tr>
                </thead>
                <tbody>
                  ${rollingRows}
                </tbody>
              </table>
            </div>
          </div>

          <div class="research-section">
            <h2>${escapeHtml(practical.title || "五、實用發現")}</h2>
            <ul class="research-strat-list">
              ${practicalItems.map((it) => `<li>${escapeHtml(it)}</li>`).join("")}
            </ul>
          </div>

          <div class="research-section">
            <h2>${escapeHtml(limitations.title || "六、研究限制與免責聲明")}</h2>
            ${(limitations.content || []).map((p) => `<p>${escapeHtml(p)}</p>`).join("")}
          </div>

          <div class="research-section disclaimer-box">
            <p>⚠️ <strong>免責聲明</strong>：${escapeHtml(disc.text || "本研究僅供策略回測與學術實證參考，非個人化投資建議。")}</p>
            <div class="github-links">
              ${ghLinks.map((g) => `<a href="${escapeHtml(g.url)}" target="_blank" rel="noopener noreferrer" class="ext-link">🔗 ${escapeHtml(g.title)}</a>`).join("")}
            </div>
          </div>
        </div>
      </section>
    `;
  }

  function renderResearchHtml(data) {
    if (!data) return '<p class="muted">深度研究資料為空。</p>';
    if (!data.articles && data.strategies_intro) {
      return renderContestResearchHtml(data);
    }
    const articles = Array.isArray(data.articles) ? data.articles : [];
    const shortArt = articles.find((a) => a.id === "short_term") || articles[0];
    const comboArt = articles.find((a) => a.id === "combo") || articles[1];
    const examArt = articles.find((a) => a.id === "exam_sensitivity") || articles[2];
    const flowArt = articles.find((a) => a.id === "flow_signals") || articles[3];
    const dcaArt = articles.find((a) => a.id === "dca_compare") || articles[4];

    return `
      <section class="card">
        <div class="section-header">
          <div>
            <h1>深度研究 (In-Depth Research)</h1>
            <p class="subtitle">${escapeHtml(data.subtitle || "Signal Desk 策略量化實證、多週期盲測與組合實驗報告")}</p>
          </div>
        </div>

        <div class="research-toc">
          <div class="research-toc-card" onclick="scrollToResearchArticle('short_term')">
            <div class="toc-card-header">
              <span class="badge badge-danger">${escapeHtml(shortArt?.badge || "全部未通過")}</span>
              <span class="toc-num">第 1 篇</span>
            </div>
            <h3 class="toc-title">${escapeHtml(shortArt?.title || "短線策略能拿去比賽嗎？")}</h3>
            <p class="toc-takeaway">${escapeHtml(shortArt?.takeaway || "")}</p>
            <div class="toc-action">點擊展開閱讀 ➔</div>
          </div>

          <div class="research-toc-card" onclick="scrollToResearchArticle('combo')">
            <div class="toc-card-header">
              <span class="badge badge-ok">${escapeHtml(comboArt?.badge || "研究完成")}</span>
              <span class="toc-num">第 2 篇</span>
            </div>
            <h3 class="toc-title">${escapeHtml(comboArt?.title || "截長補短，能不能把策略組合得更好？")}</h3>
            <p class="toc-takeaway">${escapeHtml(comboArt?.takeaway || "")}</p>
            <div class="toc-action">點擊展開閱讀 ➔</div>
          </div>

          <div class="research-toc-card" onclick="scrollToResearchArticle('exam_sensitivity')">
            <div class="toc-card-header">
              <span class="badge badge-ok">${escapeHtml(examArt?.badge || "研究完成")}</span>
              <span class="toc-num">第 3 篇</span>
            </div>
            <h3 class="toc-title">${escapeHtml(examArt?.title || "考試回撤規則放寬，結果會不一樣嗎？")}</h3>
            <p class="toc-takeaway">${escapeHtml(examArt?.takeaway || "")}</p>
            <div class="toc-action">點擊展開閱讀 ➔</div>
          </div>

          ${flowArt ? `
          <div class="research-toc-card" onclick="scrollToResearchArticle('flow_signals')">
            <div class="toc-card-header">
              <span class="badge badge-warn">${escapeHtml(flowArt.badge || "僅 2 項隔天提示")}</span>
              <span class="toc-num">第 4 篇</span>
            </div>
            <h3 class="toc-title">${escapeHtml(flowArt.title || "大戶／法人資金流回測（2026-11）")}</h3>
            <p class="toc-takeaway">${escapeHtml(flowArt.takeaway || "")}</p>
            <div class="toc-action">點擊展開閱讀 ➔</div>
          </div>
          ` : ""}

          ${dcaArt ? `
          <div class="research-toc-card" onclick="scrollToResearchArticle('dca_compare')">
            <div class="toc-card-header">
              <span class="badge badge-ok">${escapeHtml(dcaArt.badge || "10 年多標的實證")}</span>
              <span class="toc-num">第 5 篇</span>
            </div>
            <h3 class="toc-title">${escapeHtml(dcaArt.title || "定期定額 vs 單筆買進 vs 六種策略（10 年實證）")}</h3>
            <p class="toc-takeaway">${escapeHtml(dcaArt.takeaway || "")}</p>
            <div class="toc-action">點擊展開閱讀 ➔</div>
          </div>
          ` : ""}
        </div>
      </section>

      <section class="card research-article" id="article-short_term">
        <div class="section-header article-header" onclick="toggleResearchArticle('short_term')">
          <div>
            <h2>第 1 篇：${escapeHtml(shortArt?.title || "短線策略能拿去比賽嗎？")}</h2>
            <p class="subtitle">${escapeHtml(shortArt?.subtitle || "")}</p>
          </div>
          <div class="article-header-actions" onclick="event.stopPropagation()">
            <span class="badge badge-danger">${escapeHtml(shortArt?.badge || "全部未通過")}</span>
            <button type="button" class="btn btn-sm article-toggle-btn" id="toggle-btn-short_term" onclick="toggleResearchArticle('short_term')">收折</button>
          </div>
        </div>
        <div class="article-body" id="body-short_term">
          ${renderContestResearchHtml(shortArt)}
        </div>
      </section>

      ${renderComboArticleHtml(comboArt)}
      ${renderExamSensitivityArticleHtml(examArt)}
      ${renderFlowSignalsArticleHtml(flowArt)}
      ${renderDcaCompareArticleHtml(dcaArt)}
    `;
  }

  function renderContestResearchHtml(data) {
    if (!data) return '<p class="muted">短線策略研究資料為空。</p>';
    if (data.articles) {
      return renderResearchHtml(data);
    }
    const takeaway = escapeHtml(data.takeaway || "");
    const stratsIntro = Array.isArray(data.strategies_intro) ? data.strategies_intro : [];
    const meth = data.methodology || {};
    const defs = meth.definitions || {};
    const r2 = data.round2 || {};
    const tf1h = r2.tf_1h || {};
    const tf15m = r2.tf_15m || {};
    const r1 = data.round1_summary || {};
    const sim = data.contest_sim || {};
    const learnings = Array.isArray(data.learnings) ? data.learnings : [];
    const disc = data.disclaimer || {};
    const ghLinks = Array.isArray(disc.github_links) ? disc.github_links : [];

    return `
      <div class="callout callout-takeaway">
        <p>📌 <strong>核心結論：</strong>${takeaway}</p>
      </div>

      <div class="research-section">
        <h2>一、我們測了什麼？</h2>
        <p class="muted">涵蓋 7 種短線交易常見策略，包含均線、突破、順勢回踩，以及訂單塊 (Order Block) 與掃停損 (Liquidity Sweep) 等價格行為與 SMC 技術：</p>
        <ul class="research-strat-list">
          ${stratsIntro.map((s) => `<li><strong>${escapeHtml(s.name)}</strong>：${escapeHtml(s.description)}</li>`).join("")}
        </ul>
      </div>

      <div class="research-section">
        <h2>二、怎麼測的？（嚴格驗證機制）</h2>
        <ul>
          <li><strong>資料期間</strong>：${escapeHtml(meth.period || "")}</li>
          <li><strong>樣本切分</strong>：${escapeHtml(meth.split || "")}</li>
          <li><strong>交易成本</strong>：${escapeHtml(meth.costs || "")}</li>
          <li><strong>對照測試</strong>：${escapeHtml(meth.stress_and_zero || "")}</li>
          <li><strong>通過門檻</strong>：${escapeHtml(meth.criteria || "")}</li>
        </ul>
        <div class="metrics-explainer">
          <h4>📖 核心指標白話解釋：</h4>
          <ul>
            <li><strong>${escapeHtml(defs.pf || "獲利因子 (Profit Factor, PF) = 賺的總額 ÷ 賠的總額，大於 1 才有賺。")}</strong></li>
            <li><strong>${escapeHtml(defs.exp_r || "期望值 (Expectancy R) = 平均每筆交易賺賠幾倍停損。")}</strong></li>
          </ul>
        </div>
      </div>

      <div class="research-section">
        <h2>三、回測結果表格（第二輪：The5ers MT5 極低成本）</h2>
        
        <h3>1. 1 小時 K 線（1h，涵蓋全部 18 個標的）</h3>
        <p class="muted">測試標的：${escapeHtml(tf1h.symbols_list || "")}（全時段無重疊，一次只持有一單）</p>
        <div class="table-wrap">
          <table class="data-table mobile-cards">
            <thead>
              <tr>
                <th>策略</th>
                <th>交易數</th>
                <th>勝率</th>
                <th>期望值 R</th>
                <th>獲利因子 (PF)</th>
                <th>壓力測試 PF (點差×1.5)</th>
                <th>零成本 PF</th>
                <th>是否通過</th>
              </tr>
            </thead>
            <tbody>
              ${renderContestTableRows(tf1h.strategies)}
            </tbody>
          </table>
        </div>

        <h3>2. 15 分 K 線（15m，四大市場代表標的）</h3>
        <p class="muted">測試標的：${escapeHtml(tf15m.symbols_list || "")}（全時段無重疊，一次只持有一單）</p>
        <div class="table-wrap">
          <table class="data-table mobile-cards">
            <thead>
              <tr>
                <th>策略</th>
                <th>交易數</th>
                <th>勝率</th>
                <th>期望值 R</th>
                <th>獲利因子 (PF)</th>
                <th>壓力測試 PF (點差×1.5)</th>
                <th>零成本 PF</th>
                <th>是否通過</th>
              </tr>
            </thead>
            <tbody>
              ${renderContestTableRows(tf15m.strategies)}
            </tbody>
          </table>
        </div>

        <details class="round1-details">
          <summary><strong>點此展開：第一輪回測結果摘要（高手續費環境對比）</strong></summary>
          <div class="details-body">
            <p class="muted">${escapeHtml(r1.description || "")}</p>
            <div class="table-wrap">
              <table class="data-table mobile-cards">
                <thead>
                  <tr>
                    <th>策略</th>
                    <th>交易數</th>
                    <th>勝率</th>
                    <th>期望值 R</th>
                    <th>獲利因子 (PF)</th>
                    <th>壓力測試 PF</th>
                    <th>零成本 PF</th>
                    <th>是否通過</th>
                  </tr>
                </thead>
                <tbody>
                  ${renderContestTableRows(r1.strategies)}
                </tbody>
              </table>
            </div>
          </div>
        </details>
      </div>

      <div class="research-section">
        <h2>四、拿去比賽會怎樣？（競賽一週高槓桿模擬）</h2>
        <p class="muted">${escapeHtml(sim.rules || "")}</p>
        <blockquote class="quote-callout">
          <p>「${escapeHtml(sim.quote || "偶爾一週能衝很高，但接近一半的機率被淘汰——那是運氣，不是策略。")}」</p>
        </blockquote>
        <div class="table-wrap">
          <table class="data-table mobile-cards">
            <thead>
              <tr>
                <th>策略</th>
                <th>週期</th>
                <th>一週報酬中位數</th>
                <th>前 10% 報酬 (P90)</th>
                <th>出局率 (&lt;$9,000)</th>
              </tr>
            </thead>
            <tbody>
              ${renderContestSimRows(sim.rows)}
            </tbody>
          </table>
        </div>
        <div class="note-box">
          💡 <strong>${escapeHtml(sim.l2_note || "")}</strong>
        </div>
      </div>

      <div class="research-section">
        <h2>五、我們學到什麼？</h2>
        <ol class="learnings-list">
          ${learnings.map((l) => `<li>${escapeHtml(l)}</li>`).join("")}
        </ol>
      </div>

      <div class="research-section disclaimer-box">
        <p>⚠️ <strong>免責聲明</strong>：${escapeHtml(disc.text || "研究結果僅供參考，過去表現不代表未來，不構成投資建議。")}</p>
        <div class="github-links">
          ${ghLinks.map((l) => `<a href="${escapeHtml(l.url)}" target="_blank" rel="noopener noreferrer" class="ext-link">🔗 ${escapeHtml(l.title)}</a>`).join("")}
        </div>
      </div>
    `;
  }

  async function renderResearchView() {
    const container = document.getElementById("research-container");
    if (!container) return;
    if (!cachedResearch) {
      container.innerHTML = '<p class="muted">深度研究資料載入中...</p>';
      const data = await fetchResearch();
      if (!data) {
        container.innerHTML = '<p class="muted">無法載入深度研究資料，請確認 <code>data/research.json</code> 存在。</p>';
        return;
      }
    }
    const stamp = String(cachedResearch.generated_at_utc || "") + ":" + (cachedResearch.articles ? cachedResearch.articles.length : 0);
    const alreadyRendered = Boolean(container.querySelector && container.querySelector(".research-article"));
    if (!alreadyRendered || lastRenderedResearchStamp !== stamp) {
      container.innerHTML = renderResearchHtml(cachedResearch);
      lastRenderedResearchStamp = stamp;
    }
    if (pendingResearchArticleId) {
      const targetId = pendingResearchArticleId;
      pendingResearchArticleId = null;
      scrollToResearchArticle(targetId);
    } else {
      expandedResearchIds.forEach((eid) => {
        const body = document.getElementById(`body-${eid}`);
        const btn = document.getElementById(`toggle-btn-${eid}`);
        if (body && body.classList.contains("collapsed")) {
          body.classList.remove("collapsed");
          if (btn) btn.textContent = "收折";
        }
      });
    }
  }

  const NEWS_VALID_MARKETS = ["台股", "美股", "加密貨幣", "總經"];
  const NEWS_CATEGORY_TO_MARKETS = {
    "台股": ["台股"],
    "美股": ["美股"],
    "加密貨幣": ["加密貨幣"],
    "鏈上資金": ["加密貨幣"],
    "衍生品": ["加密貨幣"],
    "交易所公告": ["加密貨幣"],
    "安全事件": ["加密貨幣"],
    "總經": ["總經"],
    "聯準會": ["總經"],
  };
  const NEWS_TW_SYMBOLS = new Set(["TWII", "2330", "2454", "3711", "2317", "2382", "2308", "2881", "2882", "2891", "2603", "1301", "2412"]);
  const NEWS_US_SYMBOLS = new Set(["NAS100", "SP500", "TSLA", "GOOGL", "AAPL", "SNDK", "SPCX"]);
  const NEWS_MARKET_ALIASES = {
    all: "all",
    tw: "台股",
    us: "美股",
    crypto: "加密貨幣",
    macro: "總經",
    "台股": "台股",
    "美股": "美股",
    "加密貨幣": "加密貨幣",
    "總經": "總經",
  };

  function deriveNewsMarkets(item) {
    if (!item || typeof item !== "object") return ["總經"];
    if (Array.isArray(item.markets) && item.markets.length > 0) {
      const valid = item.markets.filter((m) => NEWS_VALID_MARKETS.includes(m));
      if (valid.length > 0) return valid;
    }
    const found = new Set(NEWS_CATEGORY_TO_MARKETS[item.category] || []);
    const syms = Array.isArray(item.symbols) ? item.symbols : [];
    for (const s of syms) {
      if (NEWS_TW_SYMBOLS.has(s)) found.add("台股");
      else if (NEWS_US_SYMBOLS.has(s)) found.add("美股");
      else if (String(s).endsWith("USDT")) found.add("加密貨幣");
    }
    const textLower = String(item.title || "").toLowerCase();
    const kwMap = {
      "台股": ["台股", "加權指數", "台積電", "台灣股市", "taiex", "taiwan stock", "tsmc"],
      "美股": ["美股", "那斯達克", "標普", "道瓊", "華爾街", "wall street", "us stocks", "u.s. stocks", "nasdaq", "s&p 500", "dow jones"],
      "加密貨幣": ["比特幣", "以太坊", "加密貨幣", "幣圈", "穩定幣", "bitcoin", "btc", "ethereum", "eth", "crypto"],
    };
    Object.entries(kwMap).forEach(([mkt, kws]) => {
      for (const kw of kws) {
        if (/^[a-z0-9.\- ]+$/.test(kw) && kw.length <= 5) {
          const re = new RegExp(`(^|[^a-z0-9])${kw.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}([^a-z0-9]|$)`);
          if (re.test(textLower)) {
            found.add(mkt);
            break;
          }
        } else if (textLower.includes(kw)) {
          found.add(mkt);
          break;
        }
      }
    });
    const ordered = NEWS_VALID_MARKETS.filter((m) => found.has(m));
    return ordered.length > 0 ? ordered : ["總經"];
  }

  function parseNewsMarketFromHash(rawHash) {
    const cleaned = String(rawHash || "").replace(/^#\/?/, "");
    const qIdx = cleaned.indexOf("?");
    const pathPart = qIdx >= 0 ? cleaned.slice(0, qIdx) : cleaned;
    const queryPart = qIdx >= 0 ? cleaned.slice(qIdx + 1) : "";
    const parts = pathPart.split("/").filter(Boolean);
    if (parts[0] !== "news") return "all";
    if (queryPart) {
      for (const kv of queryPart.split("&")) {
        const eq = kv.indexOf("=");
        if (eq < 0) continue;
        const k = kv.slice(0, eq);
        const v = kv.slice(eq + 1);
        if (k === "m") {
          try {
            return NEWS_MARKET_ALIASES[decodeURIComponent(v)] || "all";
          } catch (_) {
            return NEWS_MARKET_ALIASES[v] || "all";
          }
        }
      }
    }
    if (parts[1]) {
      try {
        return NEWS_MARKET_ALIASES[decodeURIComponent(parts[1])] || "all";
      } catch (_) {
        return NEWS_MARKET_ALIASES[parts[1]] || "all";
      }
    }
    return "all";
  }

  function markNewsVisited() {
    if (newsVisitRecorded) return;
    newsVisitRecorded = true;
    try {
      if (typeof localStorage !== "undefined") {
        const prevRaw = localStorage.getItem(NEWS_LAST_VISIT_KEY);
        const prevNum = Number(prevRaw);
        if (Number.isFinite(prevNum) && prevNum > 0) {
          newsPrevVisitMs = prevNum;
        }
        localStorage.setItem(NEWS_LAST_VISIT_KEY, String(Date.now()));
      }
    } catch (_) {}
  }

  function getNewsReaction(itemId) {
    if (!itemId || !cachedNewsReactions) return null;
    const map = cachedNewsReactions.reactions || cachedNewsReactions;
    return map && typeof map === "object" ? (map[itemId] || null) : null;
  }

  function formatReactionShortTarget(target) {
    const t = String(target || "");
    if (!t) return "";
    if (t.endsWith("USDT") && t.length > 4) return t.slice(0, -4);
    if (t === "NAS100") return "那指";
    if (t === "SP500") return "標普";
    if (t === "TWII") return "加權";
    if (t === "HSI") return "恒指";
    return t;
  }

  function renderSentimentBadgeHtml(item, clickable) {
    if (!item || !item.sentiment || !item.sentiment_label) {
      return '<span class="news-sent-btn news-sent-none" title="舊資料無規則初判">—</span>';
    }
    const sent = item.sentiment;
    const cls = sent === "bullish" ? "news-sent-bullish" : (sent === "bearish" ? "news-sent-bearish" : "news-sent-neutral");
    const reason = item.sentiment_reason || item.sentiment_label;
    if (clickable) {
      return `<button type="button" class="news-sent-btn ${cls}" data-news-id="${escapeHtml(item.id || "")}" title="${escapeHtml(reason)}">${escapeHtml(item.sentiment_label)}</button>`;
    }
    return `<span class="news-sent-btn ${cls}" title="${escapeHtml(reason)}">${escapeHtml(item.sentiment_label)}</span>`;
  }

  function renderReactionBadgeHtml(item, refNowMs) {
    const r = getNewsReaction(item && item.id);
    if (r && r.reaction_status === "ok") {
      const shortLabel = r.reaction_label === "市場解讀偏多" ? "偏多" : (r.reaction_label === "市場解讀偏空" ? "偏空" : "不明顯");
      const cls = r.reaction_direction ? `news-react-${r.reaction_direction}` : "news-react-neutral";
      const ret60 = Number(r.ret_60m_pct);
      const pctStr = Number.isFinite(ret60) ? `${ret60 >= 0 ? "+" : ""}${ret60.toFixed(1)}%` : "--";
      const tgtShort = formatReactionShortTarget(r.target || "");
      const closedNote = r.market_closed_at_event ? "（休市順延至開盤）" : "";
      const r15Note = (r.ret_15m_pct !== null && r.ret_15m_pct !== undefined && !Number.isNaN(Number(r.ret_15m_pct)))
        ? `｜15分 ${Number(r.ret_15m_pct) >= 0 ? "+" : ""}${Number(r.ret_15m_pct).toFixed(2)}%`
        : "";
      const tip = `基準：${r.base_time_taipei || "--"} 台北${closedNote}${r15Note}`;
      const mainPill = `<span class="news-react-pill ${cls}" title="${escapeHtml(tip)}">市場：${escapeHtml(shortLabel)} ${escapeHtml(tgtShort)} ${escapeHtml(pctStr)} / 60分</span>`;
      const divergePill = r.agree_with_rule === false
        ? '<span class="news-diverge-pill" title="市場 60 分鐘實際走勢與規則初判方向相反">判讀分歧</span>'
        : "";
      return mainPill + divergePill;
    }
    if (!r || r.reaction_status !== "unavailable") {
      const seenMs = Date.parse((item && (item.first_seen_at_utc || item.published_at_utc)) || "");
      if (Number.isFinite(seenMs) && (refNowMs - seenMs) < 3600 * 1000 && (refNowMs - seenMs) >= -300 * 1000) {
        return '<span class="news-react-pending">市場反應計算中</span>';
      }
    }
    return "";
  }

  function getNewsRefNowMs() {
    const updMs = Date.parse((cachedNewsLatest && (cachedNewsLatest.last_updated_utc || cachedNewsLatest.updated_at_utc)) || "") || 0;
    return Math.max(Date.now(), updMs);
  }

  function getRelatedNewsForSymbol(symbol, days = 3, limit = 5, nowMsOverride) {
    if (!symbol || !cachedNewsLatest) return [];
    const pool = []
      .concat(Array.isArray(cachedNewsLatest.latest_items) ? cachedNewsLatest.latest_items : [])
      .concat(Array.isArray(cachedNewsLatest.recent_7d_items) ? cachedNewsLatest.recent_7d_items : []);
    if (pool.length === 0) return [];
    const refMs = Number.isFinite(nowMsOverride)
      ? nowMsOverride
      : Math.max(Date.now(), Date.parse(cachedNewsLatest.updated_at_utc || "") || 0);
    const cutoffMs = refMs - days * 86400 * 1000;
    const seen = new Set();
    const matched = [];
    for (const item of pool) {
      if (!item || !item.id || seen.has(item.id)) continue;
      seen.add(item.id);
      const syms = Array.isArray(item.symbols) ? item.symbols : [];
      if (!syms.includes(symbol)) continue;
      const pubMs = Date.parse(item.published_at_utc || "");
      if (Number.isFinite(pubMs) && pubMs < cutoffMs) continue;
      matched.push(item);
    }
    matched.sort((a, b) => (Date.parse(b.published_at_utc || "") || 0) - (Date.parse(a.published_at_utc || "") || 0));
    return matched.slice(0, limit);
  }

  function renderSignalRelatedNewsHtml(symbol) {
    const items = getRelatedNewsForSymbol(symbol, 3, 5);
    if (!items || items.length === 0) return "";
    const refNowMs = getNewsRefNowMs();
    const rowsHtml = items.map((it) => {
      const stars = it.importance_stars || "★".repeat(Number(it.importance || 1));
      const starCls = `news-stars news-stars-${Math.min(3, Math.max(1, Number(it.importance || 1)))}`;
      const timeStr = formatTaipeiTime(it.published_at_utc);
      const safeUrl = escapeHtml(it.url || "#");
      const sentHtml = renderSentimentBadgeHtml(it, false);
      const reactHtml = renderReactionBadgeHtml(it, refNowMs);
      return `
        <li class="signal-news-item">
          <span class="news-time">${escapeHtml(timeStr)}</span>
          <span class="${starCls}">${escapeHtml(stars)}</span>
          <span class="news-cat-pill news-cat-${escapeHtml(it.category || "")}">${escapeHtml(it.category || "")}</span>
          ${sentHtml}
          <a href="${safeUrl}" target="_blank" rel="noopener noreferrer">${escapeHtml(it.title || "")}</a>
          ${reactHtml}
        </li>`;
    }).join("");
    return `
      <div class="signal-news-box">
        <div class="signal-news-title">相關消息（最近 3 天）</div>
        <ul class="signal-news-list">${rowsHtml}</ul>
      </div>`;
  }

  function renderNewsRowHtml(item) {
    const imp = Math.min(3, Math.max(1, Number(item.importance || 1)));
    const stars = item.importance_stars || "★".repeat(imp);
    const timeStr = formatTaipeiTime(item.published_at_utc || item.first_seen_at_utc);
    const cat = item.category || "總經";
    const syms = Array.isArray(item.symbols) ? item.symbols : [];
    const symPills = syms.map((sym) => `<span class="news-sym-pill">${escapeHtml(sym)}</span>`).join("");
    const followup = item.followup_of && item.followup_of.title
      ? `<a class="news-followup-link" href="${escapeHtml(item.followup_of.url || "#")}" target="_blank" rel="noopener noreferrer" title="前情（${escapeHtml(item.followup_of.issue || "")}）：${escapeHtml(item.followup_of.title)}">同事件後續</a>`
      : "";
    const dataCls = item.is_data_alert ? " news-row-data" : "";
    const sentHtml = renderSentimentBadgeHtml(item, true);
    const reactHtml = renderReactionBadgeHtml(item, getNewsRefNowMs());
    const seenMs = Date.parse(item.first_seen_at_utc || item.published_at_utc || "");
    const isNew = newsPrevVisitMs > 0 && Number.isFinite(seenMs) && seenMs > newsPrevVisitMs;
    const newPill = isNew ? '<span class="news-new-pill">新</span>' : "";
    const reasonExpanded = item.id && expandedNewsReasonIds.has(item.id);
    const reasonHtml = (item.sentiment_reason && reasonExpanded)
      ? `<div class="news-sent-reason">規則初判：${escapeHtml(item.sentiment_reason)}</div>`
      : "";
    return `
      <div class="news-row${dataCls}" data-news-id="${escapeHtml(item.id || "")}">
        <span class="news-time">${escapeHtml(timeStr)}</span>
        <span class="news-stars news-stars-${imp}">${escapeHtml(stars)}</span>
        <span class="news-cat-pill news-cat-${escapeHtml(cat)}">${escapeHtml(cat)}</span>
        ${sentHtml}
        ${newPill}
        <a class="news-title-link" href="${escapeHtml(item.url || "#")}" target="_blank" rel="noopener noreferrer">${escapeHtml(item.title || "")}</a>
        ${reactHtml}
        ${followup}
        ${symPills ? `<span class="news-symbols">${symPills}</span>` : ""}
        <span class="news-source">${escapeHtml(item.source || "")}</span>
        ${reasonHtml}
      </div>`;
  }

  function renderNewsHighlightRowHtml(item) {
    const imp = Math.min(3, Math.max(1, Number(item.importance || 1)));
    const stars = item.importance_stars || "★".repeat(imp);
    const timeStr = formatTaipeiTime(item.published_at_utc || item.first_seen_at_utc);
    const cat = item.category || "總經";
    const dataCls = item.is_data_alert ? " news-row-data" : "";
    return `
      <div class="news-row news-highlight-row${dataCls}" data-news-id="${escapeHtml(item.id || "")}">
        <span class="news-time">${escapeHtml(timeStr)}</span>
        <span class="news-stars news-stars-${imp}">${escapeHtml(stars)}</span>
        <span class="news-cat-pill news-cat-${escapeHtml(cat)}">${escapeHtml(cat)}</span>
        <a class="news-title-link" href="${escapeHtml(item.url || "#")}" target="_blank" rel="noopener noreferrer">${escapeHtml(item.title || "")}</a>
      </div>`;
  }

  function getNewsItemTimeMs(item) {
    if (!item) return null;
    if (item.published_at_utc) {
      const ms = Date.parse(item.published_at_utc);
      if (Number.isFinite(ms)) return ms;
    }
    if (item.first_seen_at_utc) {
      const ms = Date.parse(item.first_seen_at_utc);
      if (Number.isFinite(ms)) return ms;
    }
    return null;
  }

  function compareNewsItemsByTimeDesc(a, b) {
    const tA = getNewsItemTimeMs(a);
    const tB = getNewsItemTimeMs(b);
    if (tA !== null && tB !== null) {
      if (tB !== tA) return tB - tA;
      return 0;
    }
    if (tA !== null && tB === null) return -1;
    if (tA === null && tB !== null) return 1;
    return 0;
  }

  function _getIssueMetaFromIndex(issueId) {
    const issues = (cachedNewsIndex && Array.isArray(cachedNewsIndex.issues))
      ? cachedNewsIndex.issues
      : ((cachedNewsLatest && Array.isArray(cachedNewsLatest.recent_issues)) ? cachedNewsLatest.recent_issues : []);
    return issues.find((x) => x && x.issue === issueId) || null;
  }

  function formatNewsUpdatedText(latestDoc, isCurrent, issueMeta, targetIssue) {
    if (isCurrent && latestDoc) {
      const tpeStr = latestDoc.last_updated_taipei || latestDoc.updated_at_taipei || "--";
      const hhmm = tpeStr.length >= 16 ? tpeStr.slice(11, 16) : tpeStr;
      const hint = latestDoc.next_update_hint ? `（${latestDoc.next_update_hint}）` : "";
      return `最後更新：台北時間 ${hhmm}，約每 15 分鐘更新${hint}`;
    }
    const updDisplay = (issueMeta && issueMeta.updated_at_taipei) || targetIssue || "--";
    return `最後更新：台北時間 ${updDisplay}`;
  }

  async function renderNewsView() {
    const issueBadge = document.getElementById("news-issue-badge");
    const updatingPill = document.getElementById("news-updating-pill");
    const updatedEl = document.getElementById("news-updated-at");
    const countBadge = document.getElementById("news-count-badge");
    const failedBanner = document.getElementById("news-failed-banner");
    const highlightsEl = document.getElementById("news-highlights-list");
    const listEl = document.getElementById("news-list");
    const issueSelect = document.getElementById("news-issue-select");
    const catSelect = document.getElementById("news-filter-category");
    const symSelect = document.getElementById("news-filter-symbol");
    const impSelect = document.getElementById("news-filter-importance");
    const marketTabsEl = document.getElementById("news-market-tabs");
    const sentimentTabsEl = document.getElementById("news-sentiment-tabs");
    if (!listEl) return;

    if (!cachedNewsLatest) {
      if (issueBadge) issueBadge.textContent = "期號：尚無資料";
      if (updatingPill) updatingPill.classList.add("hidden");
      if (updatedEl) updatedEl.textContent = "最後更新：--";
      if (highlightsEl) highlightsEl.innerHTML = '<div class="empty-state">尚未載入消息面資料。</div>';
      listEl.innerHTML = '<div class="empty-state">目前尚無消息面資料（請確認已執行消息面排程或檢查網路連線）。</div>';
      const hiTitleEl = document.getElementById("news-highlights-title");
      if (hiTitleEl) hiTitleEl.textContent = "重點（0 則）";
      return;
    }

    const latestIssue = cachedNewsLatest.latest_issue || "";
    if (!newsSelectedIssue) newsSelectedIssue = latestIssue;

    // Populate issue dropdown from news_index.json (or recent_issues fallback)
    const allIssues = (cachedNewsIndex && Array.isArray(cachedNewsIndex.issues) && cachedNewsIndex.issues.length > 0)
      ? cachedNewsIndex.issues
      : (Array.isArray(cachedNewsLatest.recent_issues) ? cachedNewsLatest.recent_issues : []);

    if (issueSelect) {
      const prevVal = newsSelectedIssue || latestIssue;
      const optsHtml = allIssues.length > 0
        ? allIssues.map((iss) => {
            const label = iss.issue === latestIssue
              ? `${iss.issue}（當期更新中 · ${iss.count ?? 0} 則）`
              : `${iss.issue}（${iss.count ?? 0} 則）`;
            return `<option value="${escapeHtml(iss.issue)}">${escapeHtml(label)}</option>`;
          }).join("")
        : `<option value="${escapeHtml(latestIssue)}">${escapeHtml(latestIssue)}（當期更新中）</option>`;
      issueSelect.innerHTML = optsHtml;
      issueSelect.value = allIssues.some((x) => x.issue === prevVal) ? prevVal : latestIssue;
      newsSelectedIssue = issueSelect.value || latestIssue;
    }

    const targetIssue = newsSelectedIssue || latestIssue;
    const isCurrentIssue = targetIssue === latestIssue;
    const issueMeta = _getIssueMetaFromIndex(targetIssue);
    let issueItems = [];

    if (isCurrentIssue) {
      issueItems = Array.isArray(cachedNewsLatest.latest_items) ? cachedNewsLatest.latest_items.slice() : [];
    } else {
      const recentPool = Array.isArray(cachedNewsLatest.recent_7d_items) ? cachedNewsLatest.recent_7d_items : [];
      const fromRecent = recentPool.filter((it) => it && it.issue === targetIssue);
      const expectedCount = issueMeta && typeof issueMeta.count === "number" ? issueMeta.count : null;
      if (fromRecent.length > 0 && (expectedCount === null || fromRecent.length >= expectedCount)) {
        issueItems = fromRecent;
      } else {
        const targetMonth = (issueMeta && issueMeta.month) || targetIssue.slice(0, 7);
        if (Array.isArray(newsMonthCache[targetMonth])) {
          issueItems = newsMonthCache[targetMonth].filter((it) => it && it.issue === targetIssue);
        } else if (!newsLoadingMonth) {
          newsLoadingMonth = true;
          listEl.innerHTML = `<div class="empty-state">正在載入 ${escapeHtml(targetMonth)} 月份歷史資料...</div>`;
          try {
            const monthRows = await fetchNewsMonthJsonl(targetMonth);
            issueItems = monthRows.filter((it) => it && it.issue === targetIssue);
          } catch (err) {
            listEl.innerHTML = `<div class="empty-state">無法載入 ${escapeHtml(targetMonth)}.jsonl（${escapeHtml(err.message)}）</div>`;
            newsLoadingMonth = false;
            return;
          }
          newsLoadingMonth = false;
        }
      }
    }

    // Header metadata
    const isUpdating = isCurrentIssue && cachedNewsLatest.is_current_issue_updating !== false;
    if (issueBadge) {
      issueBadge.textContent = isUpdating ? `期號：${targetIssue || "--"}（更新中）` : `期號：${targetIssue || "--"}`;
    }
    if (updatingPill) {
      updatingPill.classList.toggle("hidden", !isUpdating);
    }
    if (updatedEl) {
      updatedEl.textContent = formatNewsUpdatedText(cachedNewsLatest, isCurrentIssue, issueMeta, targetIssue);
    }

    const failedList = isCurrentIssue
      ? (Array.isArray(cachedNewsLatest.failed_sources) ? cachedNewsLatest.failed_sources : [])
      : (issueMeta && Array.isArray(issueMeta.failed_sources) ? issueMeta.failed_sources : []);
    if (failedBanner) {
      if (failedList.length > 0) {
        const names = failedList.map((f) => escapeHtml(f.name || f.id || "")).join("、");
        failedBanner.innerHTML = `⚠️ 本期部分來源暫時無法連線（已保留其他正常來源）：<strong>${names}</strong>`;
        failedBanner.classList.remove("hidden");
      } else {
        failedBanner.classList.add("hidden");
        failedBanner.innerHTML = "";
      }
    }

    // Derive markets once per item & compute counts for top market tabs
    const itemsWithMarkets = issueItems.map((it) => ({
      item: it,
      markets: deriveNewsMarkets(it),
    }));
    const marketCounts = { all: itemsWithMarkets.length, "台股": 0, "美股": 0, "加密貨幣": 0, "總經": 0 };
    itemsWithMarkets.forEach(({ markets }) => {
      markets.forEach((m) => {
        if (marketCounts[m] !== undefined) marketCounts[m] += 1;
      });
    });

    if (marketTabsEl && typeof marketTabsEl.querySelectorAll === "function") {
      marketTabsEl.querySelectorAll(".tab").forEach((btn) => {
        const m = btn.dataset?.market || "all";
        btn.classList.toggle("active", m === newsSelectedMarket);
        const cntEl = btn.querySelector ? btn.querySelector(".news-tab-count") : null;
        if (cntEl && marketCounts[m] !== undefined) {
          cntEl.textContent = String(marketCounts[m]);
        }
      });
    }

    if (sentimentTabsEl && typeof sentimentTabsEl.querySelectorAll === "function") {
      sentimentTabsEl.querySelectorAll(".tab").forEach((btn) => {
        const s = btn.dataset?.sentiment || "all";
        btn.classList.toggle("active", s === newsSelectedSentiment);
      });
    }

    // Populate symbol filter options from current issue items
    if (symSelect) {
      const curSymVal = symSelect.value || "all";
      const symSet = new Set();
      issueItems.forEach((it) => (it.symbols || []).forEach((s) => symSet.add(s)));
      const sortedSyms = Array.from(symSet).sort();
      symSelect.innerHTML = '<option value="all">全部相關標的</option>' +
        sortedSyms.map((s) => `<option value="${escapeHtml(s)}">${escapeHtml(formatInstrumentTitle(s))}</option>`).join("");
      symSelect.value = sortedSyms.includes(curSymVal) ? curSymVal : "all";
    }

    // Filter by market & sentiment first for highlights, then full filters for list
    const marketFiltered = itemsWithMarkets.filter(({ markets }) => (
      newsSelectedMarket === "all" || markets.includes(newsSelectedMarket)
    ));

    // Filtered list (market + 9-category + symbol + importance + sentiment)
    const fCat = catSelect ? catSelect.value : "all";
    const fSym = symSelect ? symSelect.value : "all";
    const fImp = impSelect ? impSelect.value : "all";

    const filtered = marketFiltered
      .map((x) => x.item)
      .filter((it) => {
        if (fCat !== "all" && it.category !== fCat) return false;
        if (fSym !== "all" && !(Array.isArray(it.symbols) && it.symbols.includes(fSym))) return false;
        if (fImp === "3" && Number(it.importance || 1) < 3) return false;
        if (fImp === "2" && Number(it.importance || 1) < 2) return false;
        if (fImp === "1" && Number(it.importance || 1) !== 1) return false;
        if (newsSelectedSentiment !== "all" && it.sentiment !== newsSelectedSentiment) return false;
        return true;
      })
      .sort(compareNewsItemsByTimeDesc);

    // Highlights column: ★★★ items matching the EXACT same filters, sorted by time desc
    const star3Items = filtered.filter((it) => Number(it.importance || 1) >= 3);

    const hiTitleEl = document.getElementById("news-highlights-title");
    if (hiTitleEl) {
      hiTitleEl.textContent = `重點（${star3Items.length} 則）`;
    }

    if (highlightsEl) {
      const nextHiHtml = star3Items.length > 0
        ? star3Items.map(renderNewsHighlightRowHtml).join("")
        : '<div class="empty-state">目前沒有重點消息</div>';
      if (highlightsEl._lastRenderedHtml !== nextHiHtml) {
        highlightsEl.innerHTML = nextHiHtml;
        highlightsEl._lastRenderedHtml = nextHiHtml;
      }
    }

    if (countBadge) {
      countBadge.textContent = filtered.length === issueItems.length
        ? `${issueItems.length} 則`
        : `${filtered.length} / ${issueItems.length} 則`;
    }

    const nextListHtml = filtered.length > 0
      ? filtered.map(renderNewsRowHtml).join("")
      : '<div class="empty-state">此篩選條件下沒有消息。</div>';
    if (listEl._lastRenderedHtml !== nextListHtml) {
      listEl.innerHTML = nextListHtml;
      listEl._lastRenderedHtml = nextListHtml;
    }

    scanNewsGlossary();
  }

  function scanNewsGlossary() {
    const g = window.SIGNAL_DESK_GLOSSARY;
    if (!g || typeof g.scanContainers !== "function" || !g.isEnabled()) return;
    g.scanContainers(document.getElementById("news-highlights-list"), ".news-row");
    g.scanContainers(document.getElementById("news-list"), ".news-row");
  }

  function renderFlowTwSlot() {
    if (window.SIGNAL_DESK_FLOW_TW && typeof window.SIGNAL_DESK_FLOW_TW.mountFlowTwCard === "function") {
      window.SIGNAL_DESK_FLOW_TW.mountFlowTwCard("flow-tw-card-slot", cachedFlowTw, {
        researchHref: "#/research/flow_signals",
      });
    }
  }

  function renderDataExtras() {
    renderFlowTwSlot();
    if (!cachedFlowTw) {
      fetchFlowTwData()
        .then((doc) => {
          if (doc) {
            cachedFlowTw = doc;
            renderFlowTwSlot();
          }
        })
        .catch(() => {});
    }
    const evTbody = document.getElementById("events-tbody");
    if (evTbody) {
      const evs = cachedEvents?.events || [];
      evTbody.innerHTML = evs.length === 0
        ? '<tr><td colspan="3" class="muted center">目前尚無經濟事件資料。</td></tr>'
        : evs.map((ev) => `
            <tr>
              <td data-label="事件名稱"><strong>${escapeHtml(ev.title)}</strong></td>
              <td data-label="貨幣／影響市場">${escapeHtml(Array.isArray(ev.currencies) ? ev.currencies.join(", ") : (ev.currency || "--"))}</td>
              <td data-label="時間 (台北時間)">${formatTaipeiTime(ev.time_utc)}</td>
            </tr>
          `).join("");
    }
  }

  const LEGACY_ROUTE_REDIRECTS = {
    simple: "#/wall",
    stats: "#/account/stats",
    results: "#/account/results",
    risk: "#/account/risk",
    "contest-research": "#/research",
    "research-short": "#/research",
  };
  const ACCOUNT_SUBTABS = new Set(["overview", "stats", "results", "risk"]);

  function applyRoute() {
    const rawHash = window.location.hash || "#/wall";
    const cleaned = rawHash.replace(/^#\/?/, "");
    const qIdx = cleaned.indexOf("?");
    const pathOnly = qIdx >= 0 ? cleaned.slice(0, qIdx) : cleaned;
    const parts = pathOnly.split("/").filter(Boolean);
    const route = parts[0] || "wall";

    if (LEGACY_ROUTE_REDIRECTS[route]) {
      window.location.hash = LEGACY_ROUTE_REDIRECTS[route];
      return;
    }

    document.querySelectorAll(".route-view").forEach((el) => el.classList.add("hidden"));
    document.querySelectorAll(".nav-link").forEach((el) => el.classList.remove("active"));

    if (route === "signal" && parts[1]) {
      document.getElementById("view-signal")?.classList.remove("hidden");
      renderSignalDetail(decodeURIComponent(parts[1]));
    } else if (route === "account") {
      const rawSub = parts[1] || "overview";
      const sub = ACCOUNT_SUBTABS.has(rawSub) ? rawSub : "overview";
      document.getElementById("view-account")?.classList.remove("hidden");
      document.querySelector('.nav-link[data-route="account"]')?.classList.add("active");

      document.querySelectorAll("#account-subtabs .tab").forEach((el) => {
        const isActive = el.dataset?.accountTab === sub;
        el.classList.toggle("active", isActive);
        if (typeof el.setAttribute === "function") {
          el.setAttribute("aria-selected", isActive ? "true" : "false");
        }
      });
      document.querySelectorAll(".account-subpanel").forEach((panel) => {
        panel.classList.add("hidden");
      });
      document.getElementById(`account-panel-${sub}`)?.classList.remove("hidden");

      if (sub === "stats") {
        renderPerformanceStats();
      } else if (sub === "results") {
        renderBacktestResults();
      } else if (sub === "risk") {
        renderRiskCenter();
      } else {
        renderSimulatedAccount();
      }
    } else if (route === "data") {
      document.getElementById("view-data")?.classList.remove("hidden");
      document.querySelector('.nav-link[data-route="data"]')?.classList.add("active");
      renderDataExtras();
    } else if (route === "research") {
      let targetArt = parts[1] ? decodeURIComponent(parts[1]) : null;
      if (targetArt === "dca-compare" || targetArt === "dca_compare") targetArt = "dca_compare";
      if (targetArt === "flow-signals" || targetArt === "flow_signals") targetArt = "flow_signals";
      pendingResearchArticleId = targetArt;
      document.getElementById("view-research")?.classList.remove("hidden");
      document.querySelector('.nav-link[data-route="research"]')?.classList.add("active");
      renderResearchView();
    } else if (route === "news") {
      newsSelectedMarket = parseNewsMarketFromHash(rawHash);
      markNewsVisited();
      document.getElementById("view-news")?.classList.remove("hidden");
      document.querySelector('.nav-link[data-route="news"]')?.classList.add("active");
      renderNewsView();
    } else {
      document.getElementById("view-wall")?.classList.remove("hidden");
      document.querySelector('.nav-link[data-route="wall"]')?.classList.add("active");
      renderOpenNowSignals();
      renderWallCards();
    }
  }

  async function refreshLivePrices() {
    const liveLight = document.getElementById("live-light");
    const liveUpdatedEl = document.getElementById("live-updated");
    const [cryptoRes, tradFiRes] = await Promise.all([fetchLiveCrypto(), fetchLiveTradFi()]);
    livePrices = { ...cryptoRes, ...tradFiRes };

    const entries = Object.values(livePrices);
    const okCount = entries.filter((x) => x && !x.failed && x.price !== undefined).length;
    const totalLive = entries.length;
    if (okCount === 0) {
      liveLight.className = "light light-danger";
      liveLight.textContent = "即時：即時價格來源失敗";
    } else if (okCount < totalLive) {
      liveLight.className = "light light-warn";
      liveLight.textContent = `即時：部分成功 (${okCount}/${totalLive})`;
      liveUpdatedEl.textContent = formatTaipeiTime(new Date());
    } else {
      liveLight.className = "light light-ok";
      liveLight.textContent = `即時：正常 (${okCount}/${totalLive})`;
      liveUpdatedEl.textContent = formatTaipeiTime(new Date());
    }
    if (cachedInstruments.length > 0) renderInstruments();
    const curHash = window.location.hash || "#/wall";
    const curParts = curHash.replace(/^#\/?/, "").split("/").filter(Boolean);
    const curRoute = curParts[0] || "wall";
    if (curRoute === "wall" || !window.location.hash) {
      renderOpenNowSignals();
      renderWallCards();
    } else if (curRoute === "account" && (!curParts[1] || curParts[1] === "overview")) {
      renderSimulatedAccount();
    }
  }

  function getNewsKnownIds(latestDoc) {
    const ids = new Set();
    if (!latestDoc || typeof latestDoc !== "object") return ids;
    (latestDoc.latest_items || []).forEach((it) => { if (it && it.id) ids.add(it.id); });
    (latestDoc.recent_7d_items || []).forEach((it) => { if (it && it.id) ids.add(it.id); });
    return ids;
  }

  function applyPendingNewsUpdate() {
    if (!pendingNewsLatest) return;
    cachedNewsLatest = pendingNewsLatest;
    if (pendingNewsIndex) cachedNewsIndex = pendingNewsIndex;
    if (pendingNewsReactions) cachedNewsReactions = pendingNewsReactions;
    pendingNewsLatest = null;
    pendingNewsIndex = null;
    pendingNewsReactions = null;
    const banner = document.getElementById("news-new-banner");
    if (banner) {
      banner.classList.add("hidden");
      banner.textContent = "";
    }
    renderNewsView();
  }

  async function pollNewsUpdates() {
    if (!(window.location.hash || "").includes("news")) return;
    const [latestRes, indexRes, reactRes] = await Promise.allSettled([
      fetchNewsLatest(),
      fetchNewsIndex(),
      fetchNewsReactions(),
    ]);
    const newLatest = latestRes.status === "fulfilled" ? latestRes.value : null;
    const newIndex = indexRes.status === "fulfilled" ? indexRes.value : null;
    const newReact = reactRes.status === "fulfilled" ? reactRes.value : null;
    if (!newLatest) return;

    if (!cachedNewsLatest) {
      cachedNewsLatest = newLatest;
      if (newIndex) cachedNewsIndex = newIndex;
      if (newReact) cachedNewsReactions = newReact;
      renderNewsView();
      return;
    }

    const oldIds = getNewsKnownIds(cachedNewsLatest);
    const newItemsCount = (newLatest.latest_items || []).filter((it) => it && it.id && !oldIds.has(it.id)).length;
    if (newItemsCount > 0) {
      pendingNewsLatest = newLatest;
      pendingNewsIndex = newIndex;
      pendingNewsReactions = newReact;
      const banner = document.getElementById("news-new-banner");
      if (banner) {
        banner.textContent = `有 ${newItemsCount} 則新消息，點此載入`;
        banner.classList.remove("hidden");
      }
      return;
    }

    // No new items: update reactions & header timestamp without jumping the list
    if (newReact) cachedNewsReactions = newReact;
    cachedNewsLatest.last_updated_utc = newLatest.last_updated_utc || newLatest.updated_at_utc;
    cachedNewsLatest.last_updated_taipei = newLatest.last_updated_taipei || newLatest.updated_at_taipei;
    cachedNewsLatest.next_update_hint = newLatest.next_update_hint || cachedNewsLatest.next_update_hint;
    const updatedEl = document.getElementById("news-updated-at");
    if (updatedEl && (!newsSelectedIssue || newsSelectedIssue === cachedNewsLatest.latest_issue)) {
      updatedEl.textContent = formatNewsUpdatedText(cachedNewsLatest, true, null, cachedNewsLatest.latest_issue);
    }
  }

  async function refreshAll() {
    const errBox = document.getElementById("fetch-error-box");
    try {
      const [
        instRes, healthRes, controlRes, runLogRes, probeRes, signalsRes, tradesRes, watchRes, watchDailyRes, btRes,
        perfRes, riskRes, eqRes, evRes, contestRes, taRes, flowTwRes, newsLatestRes, newsIndexRes, newsReactRes,
      ] = await Promise.allSettled([
        fetchDataJson("instruments.json"),
        fetchDataJson("data_health.json"),
        fetchDataJson("control_status.json"),
        fetchDataJson("run_log.json"),
        fetchDataJson("data_probe_report.json"),
        fetchDataJson("signals_index.json"),
        fetchDataJson("trades.json"),
        fetchDataJson("watch_today.json"),
        fetchDataJson("watch_today_daily.json"),
        fetchDataJson("backtest_results.json"),
        fetchDataJson("performance_stats.json"),
        fetchDataJson("risk_status.json"),
        fetchDataJson("equity_curve.json"),
        fetchDataJson("events_calendar.json"),
        fetchResearch(),
        fetchTaData(),
        fetchFlowTwData(),
        fetchNewsLatest(),
        fetchNewsIndex(),
        fetchNewsReactions(),
      ]);

      if (flowTwRes.status === "fulfilled" && flowTwRes.value) {
        cachedFlowTw = flowTwRes.value;
        renderFlowTwSlot();
      }

      // Apply news on initial load or when user is NOT actively browsing #/news (avoid layout jump on #/news)
      const onNewsView = (window.location.hash || "").includes("news");
      if (!onNewsView || !cachedNewsLatest) {
        cachedNewsLatest = newsLatestRes.status === "fulfilled" ? newsLatestRes.value : cachedNewsLatest;
        cachedNewsIndex = newsIndexRes.status === "fulfilled" ? newsIndexRes.value : cachedNewsIndex;
        cachedNewsReactions = newsReactRes.status === "fulfilled" ? newsReactRes.value : cachedNewsReactions;
        if (onNewsView) {
          try { renderNewsView(); } catch (e) { console.error("renderNewsView 失敗：", e); }
        }
      }

      if (instRes.status === "rejected") {
        throw new Error(`無法讀取 data 分支資料（${instRes.reason.message}）。請確認已於 GitHub Actions 執行過 Market Scan。`);
      }

      errBox.classList.add("hidden");
      cachedInstruments = instRes.value.instruments || [];
      cachedSignals = signalsRes.status === "fulfilled" && Array.isArray(signalsRes.value) ? signalsRes.value : [];
      cachedTrades = tradesRes.status === "fulfilled" && Array.isArray(tradesRes.value) ? tradesRes.value : [];
      cachedWatch = watchRes.status === "fulfilled" ? watchRes.value : null;
      cachedWatchDaily = watchDailyRes.status === "fulfilled" ? watchDailyRes.value : null;
      cachedBacktest = btRes.status === "fulfilled" ? btRes.value : null;
      cachedPerf = perfRes.status === "fulfilled" ? perfRes.value : null;
      cachedRisk = riskRes.status === "fulfilled" ? riskRes.value : null;
      cachedEquity = eqRes.status === "fulfilled" ? eqRes.value : null;
      cachedEvents = evRes.status === "fulfilled" ? evRes.value : null;
      cachedTa = taRes.status === "fulfilled" ? taRes.value : null;
      if (contestRes.status === "fulfilled" && contestRes.value) {
        cachedResearch = contestRes.value;
      }
      const curHash = window.location.hash || "";
      if (curHash.includes("research") || curHash.includes("contest-research")) {
        renderResearchView();
      }

      const sigMap = {};
      for (const s of cachedSignals) {
        if (s && s.signal_id) sigMap[s.signal_id] = s;
      }
      const activeTrades = cachedTrades.filter((t) => t && t.state === "active");
      if (activeTrades.length > 0) {
        const sigResults = await Promise.allSettled(
          activeTrades.map((t) => fetchDataJson(`signals/${encodeURIComponent(t.signal_id)}.json`))
        );
        sigResults.forEach((r) => {
          if (r.status === "fulfilled" && r.value && r.value.signal_id) {
            sigMap[r.value.signal_id] = r.value;
          }
        });
      }
      cachedSignalById = sigMap;

      // 每個區塊各自防護：一個區塊出錯不會拖垮整個頁面
      const renderTasks = [
        renderOpenNowSignals, renderInstruments, renderWallCards, renderBacktestResults,
        renderPerformanceStats, renderSimulatedAccount, renderRiskCenter, renderDataExtras,
      ];
      if (!onNewsView) renderTasks.push(renderNewsView);
      renderTasks.forEach((fn) => {
        try { fn(); } catch (e) { console.error(`${fn.name} 失敗：`, e); }
      });

      const health = healthRes.status === "fulfilled" ? healthRes.value : null;
      const control = controlRes.status === "fulfilled" ? controlRes.value : null;
      updateLights(health, control);

      if (health && health.total_equity_usd !== undefined) {
        document.getElementById("total-equity").textContent = formatUsd(health.total_equity_usd);
        document.getElementById("short-equity").textContent = formatUsd(health.short_term_equity_usd);
        document.getElementById("long-equity").textContent = formatUsd(health.long_term_equity_usd);
      }
      if (runLogRes.status === "fulfilled") renderRunLog(runLogRes.value, health);
      if (probeRes.status === "fulfilled") renderProbe(probeRes.value);
      renderWatchToday();
    } catch (err) {
      errBox.textContent = `資料來源失敗：${err.message}`;
      errBox.classList.remove("hidden");
      updateLights(null, null);
      document.getElementById("instruments-tbody").innerHTML =
        '<tr><td colspan="9" class="center"><span class="pill pill-fail">資料來源失敗</span></td></tr>';
    }
  }

  document.getElementById("market-filters").addEventListener("click", (e) => {
    const btn = e.target.closest(".tab");
    if (!btn) return;
    document.querySelectorAll("#market-filters .tab").forEach((el) => el.classList.remove("active"));
    btn.classList.add("active");
    currentMarketFilter = btn.dataset.market || "all";
    renderInstruments();
  });

  document.getElementById("instruments-tbody").addEventListener("click", (e) => {
    const detailPanel = e.target.closest(".ta-panel");
    if (detailPanel) {
      // Click inside panel does not collapse row
      return;
    }
    const row = e.target.closest("tr.instrument-row");
    if (!row || !row.dataset.symbol) return;
    const sym = row.dataset.symbol;
    expandedTaSymbol = expandedTaSymbol === sym ? null : sym;
    renderInstruments();
  });

  document.getElementById("wall-status-tabs").addEventListener("click", (e) => {
    const btn = e.target.closest(".tab");
    if (!btn) return;
    document.querySelectorAll("#wall-status-tabs .tab").forEach((el) => el.classList.remove("active"));
    btn.classList.add("active");
    currentWallTab = btn.dataset.tab || "active";
    renderWallCards();
  });

  function toggleWallCardExpansion(cardEl) {
    if (!cardEl || !cardEl.dataset?.sid) return;
    const sid = cardEl.dataset.sid;
    const isNowExpanded = !expandedWallSignalIds.has(sid);
    if (isNowExpanded) {
      expandedWallSignalIds.add(sid);
    } else {
      expandedWallSignalIds.delete(sid);
    }
    cardEl.classList.toggle("expanded", isNowExpanded);
    const compactEl = cardEl.querySelector(".signal-card-compact");
    if (compactEl) compactEl.setAttribute("aria-expanded", isNowExpanded ? "true" : "false");
    const toggleBtn = cardEl.querySelector(".wall-expand-btn");
    if (toggleBtn) {
      toggleBtn.setAttribute("aria-expanded", isNowExpanded ? "true" : "false");
      toggleBtn.textContent = isNowExpanded ? "收合 ▴" : "展開 ▾";
    }
  }

  document.getElementById("wall-cards")?.addEventListener("click", async (e) => {
    const copyBtn = e.target.closest(".copy-sig-btn");
    if (copyBtn) {
      const sig = cachedSignals.find((x) => x.signal_id === copyBtn.dataset.sid);
      if (sig) {
        const ok = await copyTextToClipboard(formatSignalCopyText(sig));
        if (ok) {
          flashCopySuccess(copyBtn, "一鍵複製報單文字");
        } else {
          copyBtn.textContent = "請手動複製";
        }
      }
      return;
    }
    if (e.target.closest("a, button") && !e.target.closest(".wall-expand-btn")) return;
    const compactEl = e.target.closest(".signal-card-compact");
    if (!compactEl) return;
    toggleWallCardExpansion(compactEl.closest(".signal-card"));
  });

  document.getElementById("wall-cards")?.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" && e.key !== " ") return;
    if (e.target.closest("a, button")) return;
    const compactEl = e.target.closest(".signal-card-compact");
    if (!compactEl) return;
    e.preventDefault();
    toggleWallCardExpansion(compactEl.closest(".signal-card"));
  });

  ["wall-filter-strategy", "wall-filter-market", "wall-filter-direction", "wall-filter-symbol"].forEach((id) => {
    const el = document.getElementById(id);
    el.addEventListener("change", renderWallCards);
    el.addEventListener("input", renderWallCards);
  });

  document.getElementById("news-issue-select")?.addEventListener("change", (e) => {
    newsSelectedIssue = e.target.value || "";
    renderNewsView();
  });
  ["news-filter-category", "news-filter-symbol", "news-filter-importance"].forEach((id) => {
    document.getElementById(id)?.addEventListener("change", () => {
      renderNewsView();
    });
  });

  document.getElementById("news-market-tabs")?.addEventListener("click", (e) => {
    const btn = e.target && e.target.closest ? e.target.closest(".tab") : null;
    if (!btn) return;
    const mkt = btn.dataset?.market || "all";
    newsSelectedMarket = mkt;
    const targetHash = mkt === "all" ? "#/news" : `#/news?m=${encodeURIComponent(mkt)}`;
    if (window.location.hash !== targetHash) {
      window.location.hash = targetHash;
    }
    renderNewsView();
  });

  document.getElementById("news-sentiment-tabs")?.addEventListener("click", (e) => {
    const btn = e.target && e.target.closest ? e.target.closest(".tab") : null;
    if (!btn) return;
    newsSelectedSentiment = btn.dataset?.sentiment || "all";
    renderNewsView();
  });

  document.getElementById("news-new-banner")?.addEventListener("click", () => {
    applyPendingNewsUpdate();
  });

  const handleNewsReasonToggle = (e) => {
    const btn = e.target && e.target.closest ? e.target.closest(".news-sent-btn[data-news-id]") : null;
    if (!btn) return;
    const nid = btn.dataset?.newsId;
    if (!nid) return;
    if (expandedNewsReasonIds.has(nid)) expandedNewsReasonIds.delete(nid);
    else expandedNewsReasonIds.add(nid);
    renderNewsView();
  };
  document.getElementById("news-list")?.addEventListener("click", handleNewsReasonToggle);
  document.getElementById("news-highlights-list")?.addEventListener("click", handleNewsReasonToggle);

  const hlCard = document.getElementById("news-highlights-card");
  const hlToggle = document.getElementById("news-highlights-toggle");
  if (hlToggle && hlCard) {
    const handleHlToggle = () => {
      hlCard.classList.toggle("collapsed");
      const isCol = hlCard.classList.contains("collapsed");
      hlToggle.setAttribute("aria-expanded", String(!isCol));
    };
    hlToggle.addEventListener("click", handleHlToggle);
    hlToggle.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        handleHlToggle();
      }
    });
  }

  document.getElementById("watch-scope-tabs")?.addEventListener("click", (e) => {
    const btn = e.target.closest(".tab");
    if (!btn) return;
    document.querySelectorAll("#watch-scope-tabs .tab").forEach((el) => el.classList.remove("active"));
    btn.classList.add("active");
    currentWatchScope = btn.dataset.scope || "intraday";
    currentWatchCategory = "all";
    showAllWatch = false;
    renderWatchToday();
  });

  document.getElementById("watch-summary-bar")?.addEventListener("click", (e) => {
    const chip = e.target.closest(".watch-chip");
    if (!chip) return;
    currentWatchCategory = chip.dataset.cat || "all";
    showAllWatch = false;
    renderWatchToday();
  });

  document.getElementById("watch-toggle-btn")?.addEventListener("click", () => {
    showAllWatch = !showAllWatch;
    renderWatchToday();
  });

  document.getElementById("simple-copy-all-btn")?.addEventListener("click", async (e) => {
    if (!openNowAllCopyText) return;
    const btn = e.currentTarget;
    const ok = await copyTextToClipboard(openNowAllCopyText);
    if (ok) flashCopySuccess(btn, "複製全部");
  });

  document.getElementById("simple-open-now-grid")?.addEventListener("click", async (e) => {
    const btn = e.target.closest(".open-now-copy-btn");
    if (!btn) return;
    const sid = btn.dataset.signalId;
    const txt = sid ? openNowCopyById[sid] : "";
    if (!txt) return;
    const ok = await copyTextToClipboard(txt);
    if (ok) {
      openNowCopiedAt[sid] = Date.now();
      flashCopySuccess(btn, "複製");
    }
  });

  document.getElementById("refresh-btn").addEventListener("click", () => {
    if ((window.location.hash || "").includes("news")) {
      Promise.allSettled([fetchNewsLatest(), fetchNewsIndex(), fetchNewsReactions()]).then(([l, i, r]) => {
        if (l.status === "fulfilled" && l.value) cachedNewsLatest = l.value;
        if (i.status === "fulfilled" && i.value) cachedNewsIndex = i.value;
        if (r.status === "fulfilled" && r.value) cachedNewsReactions = r.value;
        pendingNewsLatest = null;
        pendingNewsIndex = null;
        pendingNewsReactions = null;
        const banner = document.getElementById("news-new-banner");
        if (banner) banner.classList.add("hidden");
        renderNewsView();
      });
    }
    refreshAll();
    refreshLivePrices();
  });

  window.__SIGNAL_DESK_UTILS__ = {
    SUGGESTED_LEVERAGE,
    formatNumber,
    formatExactPrice,
    getCardPriceDecimals,
    formatCardPrice,
    formatCardPricePair,
    formatPercentDisplay,
    getPricePrecision,
    formatTaipeiTime,
    formatSignalCopyText,
    convertReasonUtcToTaipei,
    classifyWatchReason,
    formatSimpleSymbolName,
    getStrategyPoolKind,
    getSimpleStrategyShortName,
    isStrategyVerified,
    extractSimpleReasonText,
    computeSimpleExitPoint,
    computeSimpleTradePnl,
    buildSimpleSignalCardsData,
    computeOpenNowHistoryStats,
    evaluateOpenNowEligibility,
    computeOpenNowPositionPct,
    computeOpenNowSizing,
    formatOpenNowSingleCopyText,
    formatOpenNowAllCopyText,
    buildOpenNowCardsData,
    renderOpenPositionsHtml,
    renderSimulatedAccount,
    renderContestResearchHtml,
    renderExamSensitivityArticleHtml,
    renderFlowSignalsArticleHtml,
    renderDcaCompareArticleHtml,
    renderResearchHtml,
    renderResearchView,
    fetchResearch,
    scrollToResearchArticle,
    toggleResearchArticle,
    applyRoute,
    getUiMarketKey,
    marketLabel,
    renderTaPanelHtml,
    renderInstruments,
    formatPriceWithCommas,
    setTaDataForTest(taObj, instrumentsArr) {
      if (taObj !== undefined) cachedTa = taObj;
      if (Array.isArray(instrumentsArr)) cachedInstruments = instrumentsArr;
    },
    toggleTaSymbolForTest(sym) {
      expandedTaSymbol = expandedTaSymbol === sym ? null : sym;
      renderInstruments();
      return expandedTaSymbol;
    },
    getExpandedTaSymbol() {
      return expandedTaSymbol;
    },
    getRelatedNewsForSymbol,
    renderSignalRelatedNewsHtml,
    renderNewsView,
    renderNewsRowHtml,
    renderNewsHighlightRowHtml,
    compareNewsItemsByTimeDesc,
    getNewsItemTimeMs,
    deriveNewsMarkets,
    parseNewsMarketFromHash,
    pollNewsUpdates,
    applyPendingNewsUpdate,
    setNewsDataForTest(latestDoc, indexDoc, monthCacheOverride, reactionsDoc, prevVisitMsOverride) {
      cachedNewsLatest = latestDoc || null;
      cachedNewsIndex = indexDoc || null;
      if (reactionsDoc !== undefined) cachedNewsReactions = reactionsDoc || null;
      if (Number.isFinite(prevVisitMsOverride)) newsPrevVisitMs = prevVisitMsOverride;
      newsSelectedIssue = (latestDoc && latestDoc.latest_issue) || "";
      if (monthCacheOverride && typeof monthCacheOverride === "object") {
        Object.keys(monthCacheOverride).forEach((m) => {
          newsMonthCache[m] = monthCacheOverride[m];
        });
      }
    },
    setNewsFiltersForTest({ market, sentiment } = {}) {
      if (market !== undefined) newsSelectedMarket = market;
      if (sentiment !== undefined) newsSelectedSentiment = sentiment;
    },
    getNewsSelectedIssue() {
      return newsSelectedIssue;
    },
    getNewsSelectedMarket() {
      return newsSelectedMarket;
    },
  };

  window.addEventListener("hashchange", applyRoute);
  window.addEventListener("resize", () => {
    if (activeEquityChart) {
      const box = document.getElementById("equity-chart-box");
      if (box && box.clientWidth > 0) {
        activeEquityChart.applyOptions({ width: box.clientWidth, height: box.clientHeight || 340 });
      }
    }
    if (activeChart) {
      const box = document.getElementById("signal-chart-box");
      if (box && box.clientWidth > 0) {
        activeChart.applyOptions({ width: box.clientWidth, height: box.clientHeight || 340 });
      }
    }
  });

  if (window.SIGNAL_DESK_GLOSSARY && typeof window.SIGNAL_DESK_GLOSSARY.initToggle === "function") {
    window.SIGNAL_DESK_GLOSSARY.initToggle("glossary-toggle-btn", () => {
      const hi = document.getElementById("news-highlights-list");
      const li = document.getElementById("news-list");
      if (hi) hi._lastRenderedHtml = null;
      if (li) li._lastRenderedHtml = null;
      renderNewsView();
      renderFlowTwSlot();
    });
  }

  applyRoute();
  refreshAll();
  refreshLivePrices();
  setInterval(refreshAll, DATA_REFRESH_MS);
  setInterval(refreshLivePrices, LIVE_REFRESH_MS);
  setInterval(pollNewsUpdates, NEWS_POLL_MS);
})();
