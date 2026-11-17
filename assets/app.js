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
  let cachedWatch = null;
  let cachedWatchDaily = null;
  let cachedBacktest = null;
  let cachedPerf = null;
  let cachedRisk = null;
  let cachedEquity = null;
  let cachedEvents = null;
  let livePrices = {};
  let activeChart = null;
  let activeEquityChart = null;

  const STRATEGY_NAMES = {
    S1: "S1 開盤區間突破（短線）",
    S2: "S2 4 小時突破（短線）",
    S3: "S3 日線 RSI 回檔（短線）",
    L1: "L1 波動度調整趨勢跟隨（長線）",
    L2: "L2 精簡標的趨勢跟隨（長線）",
    L3: "L3 雙動能（長線）",
  };

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

  function formatNumber(val) {
    if (val === null || val === undefined || Number.isNaN(Number(val))) return null;
    const num = Number(val);
    const abs = Math.abs(num);
    if (abs === 0) return "0";
    if (abs >= 1000) return num.toLocaleString("en-US", { maximumFractionDigits: 2 });
    if (abs >= 1) return num.toLocaleString("en-US", { maximumFractionDigits: 4 });
    // Keep at least 4 significant digits for small prices (e.g. PEPE 0.000004123)
    const leadingZeros = Math.max(0, -Math.floor(Math.log10(abs)) - 1);
    const decimals = Math.min(12, Math.max(4, leadingZeros + 4));
    return num.toLocaleString("en-US", { maximumFractionDigits: decimals });
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
    const strat = String(s.strategy || "S1");
    const subRow = Array.isArray(cachedRisk?.subaccounts)
      ? cachedRisk.subaccounts.find((x) => x.strategy_id === strat)
      : null;
    const defaultCap = strat.startsWith("S") ? 10000 : 23333.33;
    const subEq = Number(s.subaccount_capital_usd || subRow?.realized_equity_usd || defaultCap);
    const notional = Number(s.effective_notional_usd ?? s.notional_usd ?? 0);
    const notionalPct = s.notional_pct_subaccount !== undefined && s.notional_pct_subaccount !== null
      ? Number(s.notional_pct_subaccount)
      : (subEq > 0 ? (notional / subEq) * 100 : 0);
    const slUsd = Number(s.estimated_stop_loss_usd ?? s.risk_usd ?? 0);
    const slPct = s.estimated_stop_loss_pct_pool !== undefined && s.estimated_stop_loss_pct_pool !== null
      ? Number(s.estimated_stop_loss_pct_pool)
      : (subEq > 0 ? (slUsd / subEq) * 100 : Number(s.risk_pct_pool || 1));
    return { subEq, notional, notionalPct, slUsd, slPct };
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
    const ruleTxt = convertReasonUtcToTaipei(s.exit_rule || s.invalidation_cond || "依策略規則出場");
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

  function bindCopyButton(btn, textToCopy, labelDefault) {
    btn.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(textToCopy);
        btn.textContent = "已複製！";
        setTimeout(() => { btn.textContent = labelDefault; }, 1500);
      } catch (_) {
        btn.textContent = "請手動複製";
      }
    });
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
        const ruleTxt = convertReasonUtcToTaipei(s.exit_rule || s.invalidation_cond || "依策略規則出場");

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
    const ruleTxt = convertReasonUtcToTaipei(sig.exit_rule || sig.invalidation_cond || "依策略規則出場");
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
    const series = chart.addCandlestickSeries({
      upColor: "#2ea043",
      downColor: "#f85149",
      borderVisible: false,
      wickUpColor: "#2ea043",
      wickDownColor: "#f85149",
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

    tbody.innerHTML = list
      .map((item) => {
        const label = item.verification_label || (item.passed ? "已驗證" : (item.status === "insufficient_history" ? "資料不足，觀察中" : "觀察中（未通過驗證）"));
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
        const appTxt = `${item.eligible_instruments ?? 0} / ${item.applicable_instruments ?? 0} 檔（可用 ${yrsText}）`;
        const poolLabel = item.pool === "short_term" ? "短線" : "長線";
        const noteHtml = item.data_note ? `<div class="muted" style="margin-top:4px;font-size:0.82rem;">註：${escapeHtml(item.data_note)}</div>` : "";
        const paramsText = item.chosen_params && Object.keys(item.chosen_params).length > 0
          ? Object.entries(item.chosen_params).map(([k, v]) => `${k}=${v}`).join(", ")
          : "--";
        const detailHtml = `
          <div>${escapeHtml(item.reason || "--")}</div>
          ${noteHtml}
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
            <td data-label="策略"><strong>${escapeHtml(item.strategy_id || item.symbol)}</strong> ${escapeHtml(item.name || "")}<br><span class="muted">${poolLabel}・${escapeHtml(item.interval || "")}</span></td>
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

    const rows = [
      ["總計 (Overall)", cachedPerf.overall],
      [STRATEGY_NAMES.S1, cachedPerf.by_strategy?.S1],
      [STRATEGY_NAMES.S2, cachedPerf.by_strategy?.S2],
      [STRATEGY_NAMES.S3, cachedPerf.by_strategy?.S3],
      [STRATEGY_NAMES.L1, cachedPerf.by_strategy?.L1],
      [STRATEGY_NAMES.L2, cachedPerf.by_strategy?.L2],
      [STRATEGY_NAMES.L3, cachedPerf.by_strategy?.L3],
      ["市場：加密貨幣", cachedPerf.by_market?.crypto],
      ["市場：外匯", cachedPerf.by_market?.forex],
      ["市場：商品", cachedPerf.by_market?.commodity],
      ["市場：指數", cachedPerf.by_market?.index],
      ["方向：做多", cachedPerf.by_direction?.long],
      ["方向：做空", cachedPerf.by_direction?.short],
    ].filter(([, m]) => Boolean(m));

    if (!cachedPerf.overall.verified) {
      noticeEl.textContent = `目前總已結束報單數為 ${cachedPerf.overall.closed_trades} 筆（少於 ${minN} 筆），樣本不足，一律標示「尚未驗證」且不顯示勝率與報酬率數字。`;
      noticeEl.classList.remove("hidden");
    } else {
      noticeEl.classList.add("hidden");
    }

    tbody.innerHTML = rows
      .map(([label, m]) => {
        const verBadge = m.verified ? '<span class="pill pill-ok">已驗證</span>' : '<span class="pill pill-warn">尚未驗證</span>';
        const cntText = `${m.closed_trades || 0} / ${m.min_trades_required || minN}`;
        if (!m.verified) {
          return `
            <tr>
              <td><strong>${escapeHtml(label)}</strong></td>
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
            <td><strong>${escapeHtml(label)}</strong></td>
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

  function renderSimulatedAccount() {
    const acc = cachedRisk?.account;
    document.getElementById("acc-total-eq").textContent = acc ? formatUsd(acc.equity_usd) : "--";
    document.getElementById("acc-short-eq").textContent = acc ? formatUsd(acc.short_term_equity_usd) : "--";
    document.getElementById("acc-long-eq").textContent = acc ? formatUsd(acc.long_term_equity_usd) : "--";
    const positions = cachedRisk?.open_positions || [];
    document.getElementById("acc-open-margin").textContent = acc
      ? `${positions.length} 筆 / ${formatUsd(acc.total_open_notional_usd || 0)}`
      : "--";

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
    if (!cachedRisk) {
      posTbody.innerHTML = '<tr><td colspan="6" class="muted center">目前尚無模擬持倉資料（等待排程產生 risk_status.json）。</td></tr>';
      return;
    }
    if (positions.length === 0) {
      posTbody.innerHTML = '<tr><td colspan="6" class="muted center">目前無持倉。</td></tr>';
      return;
    }
    posTbody.innerHTML = positions
      .map((p) => {
        const pxInfo = getLivePriceInfo(p.symbol, null);
        const refPx = pxInfo.price !== null ? pxInfo.price : p.mark_price;
        let floatUsd = p.unrealized_pnl_usd || 0;
        if (pxInfo.price !== null && p.total_qty > 0 && p.avg_entry_price > 0) {
          floatUsd = p.direction === "long" ? (pxInfo.price - p.avg_entry_price) * p.total_qty : (p.avg_entry_price - pxInfo.price) * p.total_qty;
        }
        const est = computeCardEstimates(p);
        const enteredTp = formatTaipeiTime(p.entered_at_utc);
        const ruleTxt = convertReasonUtcToTaipei(p.exit_rule || "依策略規則出場");
        return `
          <tr>
            <td data-label="報單 / 標的"><a href="#/signal/${encodeURIComponent(p.signal_id)}">${escapeHtml(p.signal_id)}</a><br><strong>${escapeHtml(p.symbol)}</strong> (${p.strategy} / ${p.direction === "long" ? "多" : "空"})</td>
            <td data-label="進場時間 (台北) / 成本 / 數量">台北 ${enteredTp}<br><span class="muted">${p.avg_entry_price > 0 ? formatNumber(p.avg_entry_price) : "--"} / ${p.total_qty}</span></td>
            <td data-label="倉位名目 / 佔策略資金">$${fmtCompactUsd(est.notional)}<br><span class="muted">佔 ${escapeHtml(p.strategy)} ${est.notionalPct.toFixed(1)}%</span></td>
            <td data-label="即時現價 / 未實現損益">${formatNumber(refPx) || "--"} (${escapeHtml(pxInfo.source)})<br><span class="${floatUsd >= 0 ? "chg-up" : "chg-down"}">${formatUsd(floatUsd)}</span></td>
            <td data-label="止損 / 預估 SL 虧損">${formatNumber(p.current_stop_loss)}<br><span class="chg-down">-$${fmtCompactUsd(est.slUsd)} (${est.slPct.toFixed(1)}%)</span></td>
            <td data-label="出場規則">${escapeHtml(ruleTxt)}</td>
          </tr>
        `;
      })
      .join("");
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
    if (curHash.startsWith("#/wall") || !window.location.hash) renderWallCards();
    if (curHash.startsWith("#/account")) renderSimulatedAccount();
  }

  async function refreshAll() {
    const errBox = document.getElementById("fetch-error-box");
    try {
      const [
        instRes, healthRes, controlRes, runLogRes, probeRes, signalsRes, watchRes, watchDailyRes, btRes,
        perfRes, riskRes, eqRes, evRes,
      ] = await Promise.allSettled([
        fetchDataJson("instruments.json"),
        fetchDataJson("data_health.json"),
        fetchDataJson("control_status.json"),
        fetchDataJson("run_log.json"),
        fetchDataJson("data_probe_report.json"),
        fetchDataJson("signals_index.json"),
        fetchDataJson("watch_today.json"),
        fetchDataJson("watch_today_daily.json"),
        fetchDataJson("backtest_results.json"),
        fetchDataJson("performance_stats.json"),
        fetchDataJson("risk_status.json"),
        fetchDataJson("equity_curve.json"),
        fetchDataJson("events_calendar.json"),
      ]);

      if (instRes.status === "rejected") {
        throw new Error(`無法讀取 data 分支資料（${instRes.reason.message}）。請確認已於 GitHub Actions 執行過 Market Scan。`);
      }

      errBox.classList.add("hidden");
      cachedInstruments = instRes.value.instruments || [];
      cachedSignals = signalsRes.status === "fulfilled" && Array.isArray(signalsRes.value) ? signalsRes.value : [];
      cachedWatch = watchRes.status === "fulfilled" ? watchRes.value : null;
      cachedWatchDaily = watchDailyRes.status === "fulfilled" ? watchDailyRes.value : null;
      cachedBacktest = btRes.status === "fulfilled" ? btRes.value : null;
      cachedPerf = perfRes.status === "fulfilled" ? perfRes.value : null;
      cachedRisk = riskRes.status === "fulfilled" ? riskRes.value : null;
      cachedEquity = eqRes.status === "fulfilled" ? eqRes.value : null;
      cachedEvents = evRes.status === "fulfilled" ? evRes.value : null;

      // 每個區塊各自防護：一個區塊出錯不會拖垮整個頁面
      [renderInstruments, renderWallCards, renderResultsTable, renderBacktestResults,
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

  document.getElementById("refresh-btn").addEventListener("click", () => {
    refreshAll();
    refreshLivePrices();
  });

  window.__SIGNAL_DESK_UTILS__ = {
    formatNumber,
    formatTaipeiTime,
    formatSignalCopyText,
    convertReasonUtcToTaipei,
    classifyWatchReason,
  };

  window.addEventListener("hashchange", applyRoute);

  applyRoute();
  refreshAll();
  refreshLivePrices();
  setInterval(refreshAll, DATA_REFRESH_MS);
  setInterval(refreshLivePrices, LIVE_REFRESH_MS);
})();
