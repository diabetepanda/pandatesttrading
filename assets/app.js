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
  let cachedContestResearch = null;
  let livePrices = {};
  let activeChart = null;
  let activeEquityChart = null;
  let openNowCopyById = {};
  let openNowCopiedAt = {};
  let openNowAllCopyText = "";

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

  function marketLabel(market) {
    const map = { crypto: "加密貨幣", forex: "外匯", commodity: "商品", index: "指數", stock: "股票" };
    return map[market] || market;
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

  function renderSimpleSignals() {
    renderOpenNowSignals();
    const grid = document.getElementById("simple-signals-grid");
    if (!grid) return;
    const cards = buildSimpleSignalCardsData(
      cachedTrades,
      cachedSignalById,
      cachedBacktest,
      (sym) => getDisplayPrice(sym).price,
      cachedInstruments
    );
    if (cards.length === 0) {
      grid.innerHTML = '<div class="empty-state">目前沒有持倉</div>';
      return;
    }
    grid.innerHTML = cards
      .map((c) => `
        <article class="simple-card">
          <div class="simple-card-title">${c.dot} ${escapeHtml(c.displayName)} ${c.dirZh}</div>
          <div class="simple-card-line">理由：${escapeHtml(c.reasonLine)}</div>
          <div class="simple-card-line">進場價：${escapeHtml(c.entryStr)}</div>
          <div class="simple-card-line">停損價：${escapeHtml(c.slStr)}</div>
          <div class="simple-card-line">出場點位：${escapeHtml(c.exitStr)}</div>
          <div class="simple-card-line">倉位：${escapeHtml(c.posText)}</div>
          <div class="simple-card-line">目前損益：<span class="${c.pnlInfo.cls}">${escapeHtml(c.pnlInfo.text)}</span></div>
        </article>
      `)
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
    const fStrat = document.getElementById("wall-filter-strategy").value;
    const fMkt = document.getElementById("wall-filter-market").value;
    const fDir = document.getElementById("wall-filter-direction").value;
    const fSym = document.getElementById("wall-filter-symbol").value.trim().toUpperCase();
    const cutoff30 = Date.now() - 30 * 86400 * 1000;

    const filtered = cachedSignals.filter((s) => {
      if (currentWallTab === "active" && s.lifecycle_state !== "active") return false;
      if (currentWallTab === "closed30") {
        if (s.lifecycle_state !== "closed" && s.lifecycle_state !== "cancelled") return false;
        const t = new Date(s.closed_at_utc || s.published_at_utc).getTime();
        if (Number.isNaN(t) || t < cutoff30) return false;
      }
      if (fStrat !== "all" && s.strategy !== fStrat) return false;
      if (fMkt !== "all" && s.market !== fMkt) return false;
      if (fDir !== "all" && s.direction !== fDir) return false;
      if (fSym && !s.symbol.toUpperCase().includes(fSym)) return false;
      return true;
    });

    if (filtered.length === 0) {
      container.innerHTML = '<div class="empty-state">目前此分類沒有符合條件的報單（沒有假報單；系統僅於策略與風控全數通過時發布）。</div>';
      return;
    }

    container.innerHTML = filtered
      .map((s) => {
        const dirBadge = s.direction === "long" ? '<span class="pill pill-ok">做多</span>' : '<span class="pill pill-fail">做空</span>';
        const pxInfo = getDisplayPrice(s.symbol);
        const liveStr = pxInfo.price !== null ? formatNumber(pxInfo.price) : "--";
        const floatPnl = computeFloatingPnl(s, pxInfo.price);
        const curSl = s.current_stop_loss ?? s.stop_loss;
        const entryPx = Number(s.avg_entry_price || s.entry_price || s.entry_low || 0);
        const entryStr = formatNumber(entryPx) || String(entryPx);
        const enteredTp = formatTaipeiTime(s.entered_at_utc || s.published_at_utc);
        const ruleTxt = getStrategyExitRuleText(s);

        let liveHtml = "";
        if (s.lifecycle_state === "closed" || s.lifecycle_state === "cancelled") {
          const rVal = Number(s.r_multiple || 0);
          const rCls = rVal > 0 ? "chg-up" : rVal < 0 ? "chg-down" : "muted";
          const exitLbl = s.exit_reason_label || exitReasonLabel(s.exit_reason);
          liveHtml = `
            <div class="live-box">
              <div><strong>實際損益（fills 加總）：</strong><span class="${rCls}">${rVal.toFixed(2)}R / ${formatUsd(s.realized_pnl_usd)}</span></div>
              <div class="muted">出場原因：${escapeHtml(exitLbl)}｜結束時間：台北 ${formatTaipeiTime(s.closed_at_utc || s.published_at_utc)}</div>
            </div>`;
        } else {
          const floatTxt = floatPnl
            ? `<div><strong>即時浮動損益：</strong><span class="${floatPnl.usd >= 0 ? "chg-up" : "chg-down"}">${floatPnl.r >= 0 ? "+" : ""}${floatPnl.r.toFixed(2)}R (${formatUsd(floatPnl.usd)})</span></div>`
            : "";
          const trailLine = s.trailing_exit_price
            ? `<div>目前移動出場線：${formatNumber(s.trailing_exit_price)}</div>`
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

        const est = computeCardEstimates(s);
        const verLabel = s.verification_status || "觀察中（未通過驗證）";
        const verCls = verLabel === "已驗證" ? "pill-ok" : "pill-warn";

        return `
          <article class="signal-card" data-sid="${s.signal_id}">
            <div class="signal-card-head">
              <div>
                <div class="signal-card-title">${s.symbol} ${dirBadge}</div>
                <div class="muted">${s.signal_id}｜${STRATEGY_NAMES[s.strategy] || `策略 ${s.strategy}`}｜台北 ${enteredTp}</div>
              </div>
              <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center;">
                <span class="pill ${verCls}">${escapeHtml(verLabel)}</span>
                <span class="pill pill-live">${lifecycleLabel(s.lifecycle_state)}</span>
              </div>
            </div>
            <div class="badge badge-warn" style="align-self:flex-start;">模擬驗證中，非投資建議</div>
            <div class="signal-kv">
              <div><span class="muted">進場：</span>市價 ${entryStr}（台北 ${enteredTp} 成交）</div>
              <div><span class="muted">倉位名目：</span>$${fmtCompactUsd(est.notional)}（佔 ${s.strategy} 資金 ${est.notionalPct.toFixed(1)}%）</div>
              <div><span class="muted">止損：</span>${formatNumber(curSl) || curSl}</div>
              <div><span class="muted">預估 SL 虧損：</span><span class="chg-down">-$${fmtCompactUsd(est.slUsd)}（佔 ${s.strategy} 資金 ${est.slPct.toFixed(1)}%）</span></div>
              <div style="grid-column:1 / -1;"><span class="muted">出場規則：</span>${escapeHtml(ruleTxt)}</div>
            </div>
            ${liveHtml}
            <div class="card-actions">
              <button class="btn copy-sig-btn" data-sid="${s.signal_id}" type="button">一鍵複製報單文字</button>
              <a class="btn btn-primary" href="#/signal/${encodeURIComponent(s.signal_id)}">查看詳情</a>
            </div>
          </article>`;
      })
      .join("");

    container.querySelectorAll(".copy-sig-btn").forEach((btn) => {
      const sig = cachedSignals.find((x) => x.signal_id === btn.dataset.sid);
      if (sig) bindCopyButton(btn, formatSignalCopyText(sig), "一鍵複製報單文字");
    });
  }

  async function renderSignalDetail(signalId) {
    const root = document.getElementById("signal-detail-root");
    root.innerHTML = '<div class="card muted">載入報單詳情中...</div>';

    let sig = cachedSignals.find((s) => s.signal_id === signalId) || null;
    if (!sig) {
      try {
        sig = await fetchDataJson(`signals/${encodeURIComponent(signalId)}.json`);
      } catch (err) {
        root.innerHTML = `<div class="card"><p class="error-box">找不到報單 ${signalId}（${err.message}）</p><a class="btn" href="#/wall">返回報單牆</a></div>`;
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
            <a class="btn" href="#/wall">返回報單牆</a>
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

  function formatDuration(startIso, endIso) {
    if (!startIso || !endIso) return "--";
    const ms = new Date(endIso).getTime() - new Date(startIso).getTime();
    if (Number.isNaN(ms) || ms < 0) return "--";
    const mins = Math.round(ms / 60000);
    if (mins < 60) return `${mins} 分`;
    const hrs = Math.floor(mins / 60);
    const rem = mins % 60;
    return `${hrs} 小時 ${rem} 分`;
  }

  function renderResultsTable() {
    const tbody = document.getElementById("results-tbody");
    const fStratEl = document.getElementById("res-filter-strategy");
    if (!tbody || !fStratEl) return;
    const fStrat = fStratEl.value;
    const fMkt = document.getElementById("res-filter-market")?.value || "all";
    const fDir = document.getElementById("res-filter-direction")?.value || "all";
    const fReason = document.getElementById("res-filter-reason")?.value || "all";

    const rows = cachedSignals.filter((s) => {
      if (s.lifecycle_state !== "closed" && s.lifecycle_state !== "cancelled") return false;
      if (fStrat !== "all" && s.strategy !== fStrat) return false;
      if (fMkt !== "all" && s.market !== fMkt) return false;
      if (fDir !== "all" && s.direction !== fDir) return false;
      if (fReason !== "all" && s.exit_reason !== fReason) return false;
      return true;
    });

    if (rows.length === 0) {
      tbody.innerHTML = '<tr><td colspan="9" class="muted center">目前尚無符合條件的已結束報單。</td></tr>';
      return;
    }

    tbody.innerHTML = rows
      .map((s) => {
        const rVal = Number(s.r_multiple || 0);
        const rCls = rVal > 0 ? "chg-up" : rVal < 0 ? "chg-down" : "muted";
        return `
          <tr>
            <td data-label="報單編號"><a href="#/signal/${encodeURIComponent(s.signal_id)}" style="color:var(--accent);">${s.signal_id}</a></td>
            <td data-label="策略">策略 ${s.strategy}</td>
            <td data-label="標的"><strong>${s.symbol}</strong></td>
            <td data-label="方向">${s.direction === "long" ? "做多" : "做空"}</td>
            <td data-label="R 值" class="${rCls}">${rVal.toFixed(2)}R</td>
            <td data-label="美元損益" class="${rCls}">${formatUsd(s.realized_pnl_usd)}</td>
            <td data-label="持倉時間">${formatDuration(s.entered_at_utc, s.closed_at_utc)}</td>
            <td data-label="出場原因">${escapeHtml(s.exit_reason_label || exitReasonLabel(s.exit_reason))}</td>
            <td data-label="結束時間 (台北)">${formatTaipeiTime(s.closed_at_utc)}</td>
          </tr>`;
      })
      .join("");
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

  function renderInstruments() {
    const tbody = document.getElementById("instruments-tbody");
    const rows = cachedInstruments.filter((item) => currentMarketFilter === "all" || item.market === currentMarketFilter);
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
        return `
          <tr>
            <td data-label="標的"><strong>${item.symbol}</strong><br><span class="muted">${item.display_name || ""}</span></td>
            <td data-label="市場">${marketLabel(item.market)}</td>
            <td data-label="最新價格">${renderPriceCell(item)}</td>
            <td data-label="24h 漲跌">${chgHtml}</td>
            <td data-label="排程來源">${item.source_used || '<span class="muted">無</span>'}</td>
            <td data-label="1m">${statusPill(r1m.status)}</td>
            <td data-label="15m">${statusPill(r15m.status)}</td>
            <td data-label="1d">${statusPill(r1d.status)}</td>
            <td data-label="狀態 / 備註">${statusPill(item.overall_status)} <span class="muted">${escapeHtml(note)}</span></td>
          </tr>`;
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
    const items = watchData && Array.isArray(watchData.items) ? watchData.items : [];
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
            <td data-label="標的"><strong>${escapeHtml(w.symbol)}</strong></td>
            <td data-label="市場">${marketLabel(w.market)}</td>
            <td data-label="判定">${badge}</td>
            <td data-label="波動 / 狀態指標">${indTxt}</td>
            <td data-label="各策略目前判斷與原因">${strategiesHtml}</td>
          </tr>`;
      })
      .join("");
  }

  function renderProbe(probeData) {
    const summaryEl = document.getElementById("probe-summary");
    const tableEl = document.getElementById("probe-table");
    const tbody = document.getElementById("probe-tbody");
    if (!probeData || !Array.isArray(probeData.results) || probeData.results.length === 0) {
      summaryEl.textContent = "尚無實測報告（請先於 GitHub Actions 手動觸發一次 Market Scan）。";
      tableEl.classList.add("hidden");
      return;
    }
    summaryEl.textContent = `實測時間 (台北)：${formatTaipeiTime(probeData.generated_at_utc)}，共 ${probeData.results.length} 組測試。`;
    tableEl.classList.remove("hidden");
    tbody.innerHTML = probeData.results
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
      sumEl.textContent = `近 24 小時排程執行次數：${health.runs_last_24h} 次｜自動排程時間：每小時台北時間 :15/:30/:45/:00 前後`;
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

  function formatPctOrUnverified(val, verified, suffix = "%") {
    if (!verified || val === null || val === undefined) return unverifiedCell();
    return `${Number(val).toFixed(2)}${suffix}`;
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
      ["市場：外匯", cachedPerf.by_market?.forex, false],
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
        activeEquityChart.remove();
        activeEquityChart = null;
      }
      const pts = cachedEquity?.points || [];
      if (pts.length === 0 || !window.LightweightCharts) {
        chartBox.innerHTML = "";
        chartNote.textContent = pts.length === 0 ? "目前尚無淨值快照資料（等待排程產生 equity_curve.json）。" : "圖表函式庫載入失敗。";
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
      const resp = await fetch(`assets/research.json?t=${Date.now()}`);
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

  async function fetchContestResearch() {
    if (cachedContestResearch) return cachedContestResearch;
    try {
      const resp = await fetch(`data/contest_research.json?t=${Date.now()}`);
      if (resp.ok) {
        cachedContestResearch = await resp.json();
        return cachedContestResearch;
      }
    } catch (_) {}
    try {
      const resp = await fetch(`assets/contest_research.json?t=${Date.now()}`);
      if (resp.ok) {
        cachedContestResearch = await resp.json();
        return cachedContestResearch;
      }
    } catch (_) {}
    try {
      cachedContestResearch = await fetchDataJson("contest_research.json");
      return cachedContestResearch;
    } catch (err) {
      console.warn("Failed to load contest research json:", err);
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
    const body = document.getElementById(`body-${id}`);
    const btn = document.getElementById(`toggle-btn-${id}`);
    if (body && body.classList.contains("collapsed")) {
      body.classList.remove("collapsed");
      if (btn) btn.textContent = "收折";
    }
    const el = document.getElementById(`article-${id}`);
    if (el) {
      el.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }
  window.scrollToResearchArticle = scrollToResearchArticle;

  function toggleResearchArticle(id) {
    const body = document.getElementById(`body-${id}`);
    const btn = document.getElementById(`toggle-btn-${id}`);
    if (!body) return;
    const isCollapsed = body.classList.toggle("collapsed");
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

  function renderResearchHtml(data) {
    if (!data) return '<p class="muted">深度研究資料為空。</p>';
    if (!data.articles && data.strategies_intro) {
      return renderContestResearchHtml(data);
    }
    const articles = Array.isArray(data.articles) ? data.articles : [];
    const shortArt = articles.find((a) => a.id === "short_term") || articles[0];
    const comboArt = articles.find((a) => a.id === "combo") || articles[1];
    const examArt = articles.find((a) => a.id === "exam_sensitivity") || articles[2];

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
    const container = document.getElementById("research-container") || document.getElementById("contest-research-container");
    if (!container) return;
    if (!cachedResearch) {
      container.innerHTML = '<p class="muted">深度研究資料載入中...</p>';
      const data = await fetchResearch();
      if (!data) {
        container.innerHTML = '<p class="muted">無法載入深度研究資料，請確認 <code>data/research.json</code> 存在。</p>';
        return;
      }
    }
    container.innerHTML = renderResearchHtml(cachedResearch);
  }

  async function renderContestResearchView() {
    return renderResearchView();
  }

  function renderDataExtras() {
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

  function applyRoute() {
    const rawHash = window.location.hash || "#/wall";
    const cleaned = rawHash.replace(/^#\/?/, "");
    const parts = cleaned.split("/");
    const route = parts[0] || "wall";

    document.querySelectorAll(".route-view").forEach((el) => el.classList.add("hidden"));
    document.querySelectorAll(".nav-link").forEach((el) => el.classList.remove("active"));

    if (route === "signal" && parts[1]) {
      document.getElementById("view-signal").classList.remove("hidden");
      renderSignalDetail(decodeURIComponent(parts[1]));
    } else if (route === "simple") {
      document.getElementById("view-simple")?.classList.remove("hidden");
      document.querySelector('.nav-link[data-route="simple"]')?.classList.add("active");
      renderSimpleSignals();
    } else if (route === "results") {
      document.getElementById("view-results").classList.remove("hidden");
      document.querySelector('.nav-link[data-route="results"]')?.classList.add("active");
      renderResultsTable();
      renderBacktestResults();
    } else if (route === "stats") {
      document.getElementById("view-stats").classList.remove("hidden");
      document.querySelector('.nav-link[data-route="stats"]')?.classList.add("active");
      renderPerformanceStats();
    } else if (route === "account") {
      document.getElementById("view-account").classList.remove("hidden");
      document.querySelector('.nav-link[data-route="account"]')?.classList.add("active");
      renderSimulatedAccount();
    } else if (route === "risk") {
      document.getElementById("view-risk").classList.remove("hidden");
      document.querySelector('.nav-link[data-route="risk"]')?.classList.add("active");
      renderRiskCenter();
    } else if (route === "data") {
      document.getElementById("view-data").classList.remove("hidden");
      document.querySelector('.nav-link[data-route="data"]')?.classList.add("active");
      renderDataExtras();
    } else if (route === "contest-research" || route === "research-short") {
      window.location.hash = "#/research";
      return;
    } else if (route === "research") {
      document.getElementById("view-research")?.classList.remove("hidden");
      document.querySelector('.nav-link[data-route="research"]')?.classList.add("active");
      renderResearchView();
    } else {
      document.getElementById("view-wall").classList.remove("hidden");
      document.querySelector('.nav-link[data-route="wall"]')?.classList.add("active");
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
    if (curHash.startsWith("#/simple")) renderSimpleSignals();
    if (curHash.startsWith("#/wall") || !window.location.hash) renderWallCards();
    if (curHash.startsWith("#/account")) renderSimulatedAccount();
  }

  async function refreshAll() {
    const errBox = document.getElementById("fetch-error-box");
    try {
      const [
        instRes, healthRes, controlRes, runLogRes, probeRes, signalsRes, tradesRes, watchRes, watchDailyRes, btRes,
        perfRes, riskRes, eqRes, evRes, contestRes,
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
        fetchContestResearch(),
      ]);

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
      if (contestRes.status === "fulfilled" && contestRes.value) {
        if (contestRes.value.articles) {
          cachedResearch = contestRes.value;
        } else {
          cachedContestResearch = contestRes.value;
        }
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
      [renderSimpleSignals, renderInstruments, renderWallCards, renderResultsTable, renderBacktestResults,
       renderPerformanceStats, renderSimulatedAccount,
       renderRiskCenter, renderDataExtras].forEach((fn) => {
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

  document.getElementById("wall-status-tabs").addEventListener("click", (e) => {
    const btn = e.target.closest(".tab");
    if (!btn) return;
    document.querySelectorAll("#wall-status-tabs .tab").forEach((el) => el.classList.remove("active"));
    btn.classList.add("active");
    currentWallTab = btn.dataset.tab || "active";
    renderWallCards();
  });

  ["wall-filter-strategy", "wall-filter-market", "wall-filter-direction", "wall-filter-symbol"].forEach((id) => {
    const el = document.getElementById(id);
    el.addEventListener("change", renderWallCards);
    el.addEventListener("input", renderWallCards);
  });

  ["res-filter-strategy", "res-filter-market", "res-filter-direction", "res-filter-reason"].forEach((id) => {
    document.getElementById(id)?.addEventListener("change", renderResultsTable);
  });

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
    renderContestResearchView,
    fetchContestResearch,
    renderExamSensitivityArticleHtml,
    renderResearchHtml,
    renderResearchView,
    fetchResearch,
    scrollToResearchArticle,
    toggleResearchArticle,
    applyRoute,
  };

  window.addEventListener("hashchange", applyRoute);

  applyRoute();
  refreshAll();
  refreshLivePrices();
  setInterval(refreshAll, DATA_REFRESH_MS);
  setInterval(refreshLivePrices, LIVE_REFRESH_MS);
})();
