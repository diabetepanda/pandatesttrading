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
  let cachedInstruments = [];
  let cachedSignals = [];
  let cachedWatch = null;
  let cachedBacktest = null;
  let livePrices = {};
  let activeChart = null;

  function formatTaipeiTime(isoOrDate) {
    if (!isoOrDate) return "--";
    const dt = isoOrDate instanceof Date ? isoOrDate : new Date(isoOrDate);
    if (Number.isNaN(dt.getTime())) return String(isoOrDate);
    return dt.toLocaleString("zh-TW", {
      timeZone: "Asia/Taipei",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    });
  }

  function formatNumber(val) {
    if (val === null || val === undefined || Number.isNaN(Number(val))) return null;
    const num = Number(val);
    if (num >= 1000) return num.toLocaleString("en-US", { maximumFractionDigits: 2 });
    if (num >= 1) return num.toLocaleString("en-US", { maximumFractionDigits: 4 });
    return num.toLocaleString("en-US", { maximumFractionDigits: 6 });
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
    return '<span class="pill pill-fail">資料來源失敗</span>';
  }

  function lifecycleLabel(st) {
    const map = {
      waiting_entry: "等待入場",
      active: "進行中",
      tp1_hit: "已達 TP1",
      closed: "已結束",
      cancelled: "未成交取消",
    };
    return map[st] || st;
  }

  function marketLabel(market) {
    const map = { crypto: "加密貨幣", forex: "外匯", commodity: "商品", stock: "股票" };
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

  async function fetchLiveTradFi() {
    const results = {};
    const tvSymbols = Object.keys(LIVE_TRADINGVIEW_SYMBOLS);
    await Promise.all(
      tvSymbols.map(async (sym) => {
        const metalCode = LIVE_METAL_SYMBOLS[sym];
        const tvCode = LIVE_TRADINGVIEW_SYMBOLS[sym];
        if (metalCode) {
          const [goldRes, tvRes] = await Promise.allSettled([
            fetchWithTimeout(`https://api.gold-api.com/price/${metalCode}`),
            fetchTradingViewSymbol(tvCode),
          ]);
          const tvOk = tvRes.status === "fulfilled" ? tvRes.value : null;
          if (goldRes.status === "fulfilled" && goldRes.value && goldRes.value.price !== undefined) {
            results[sym] = {
              price: Number(goldRes.value.price),
              changePct: tvOk ? tvOk.changePct : null,
              source: "gold-api",
            };
          } else if (tvOk) {
            results[sym] = tvOk;
          } else {
            results[sym] = { failed: true };
          }
          return;
        }
        try {
          const tvOk = await fetchTradingViewSymbol(tvCode);
          results[sym] = tvOk || { failed: true };
        } catch (_) {
          results[sym] = { failed: true };
        }
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
    if (livePrice === null || (sig.lifecycle_state !== "active" && sig.lifecycle_state !== "tp1_hit")) {
      return null;
    }
    const avg = Number(sig.avg_entry_price || 0);
    const remQty = Number(sig.remaining_qty || 0);
    if (avg <= 0 || remQty <= 0) return null;
    const unrealized = sig.direction === "long" ? (livePrice - avg) * remQty : (avg - livePrice) * remQty;
    const totalEst = Number(sig.realized_pnl_usd || 0) + unrealized;
    const rPlan = Number(sig.r_plan_usd || sig.risk_usd || 1);
    const estR = rPlan > 0 ? totalEst / rPlan : 0;
    return { usd: totalEst, r: estR };
  }

  function formatSignalCopyText(s) {
    const dirZh = s.direction === "long" ? "做多" : "做空";
    const stratZh = s.strategy === "A" ? "策略 A（短線）" : "策略 B（長線）";
    const adds = Array.isArray(s.add_levels) && s.add_levels.length > 0
      ? s.add_levels.map((l) => `階段${l.stage} ${l.add_type === "mean_add" ? "逆勢補倉" : "順勢加倉"} ${l.price}`).join("、")
      : "無";
    return [
      `【Signal Desk 報單】${s.signal_id}（${stratZh}）`,
      `標的：${s.symbol}｜方向：${dirZh}`,
      `入場區間：${s.entry_low} ~ ${s.entry_high}`,
      `補倉價位：${adds}`,
      `止損價：${s.stop_loss}`,
      `止盈 TP1：${s.tp1}｜TP2：${s.tp2}`,
      `建議風險比例：佔資金池 ${s.risk_pct_pool}%（預估風險 $${s.risk_usd}）`,
      `掛單有效期限：${s.valid_until_utc}`,
      `失效條件：${s.invalidation_cond}`,
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
    const activeCount = cachedSignals.filter((s) => s.lifecycle_state === "active" || s.lifecycle_state === "tp1_hit").length;

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
      if (currentWallTab === "active" && s.lifecycle_state !== "active" && s.lifecycle_state !== "tp1_hit") return false;
      if (currentWallTab === "waiting" && s.lifecycle_state !== "waiting_entry") return false;
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
        const curTp1 = s.current_tp1 ?? s.tp1;
        const curTp2 = s.current_tp2 ?? s.tp2;

        let liveHtml = "";
        if (s.lifecycle_state === "closed" || s.lifecycle_state === "cancelled") {
          const rVal = Number(s.r_multiple || 0);
          const rCls = rVal > 0 ? "chg-up" : rVal < 0 ? "chg-down" : "muted";
          liveHtml = `
            <div class="live-box">
              <div><strong>最終成績：</strong><span class="${rCls}">${rVal.toFixed(2)}R / ${formatUsd(s.realized_pnl_usd)}</span></div>
              <div class="muted">出場原因：${s.exit_reason || "--"}</div>
            </div>`;
        } else {
          const floatTxt = floatPnl
            ? `<div><strong>即時浮動損益：</strong><span class="${floatPnl.usd >= 0 ? "chg-up" : "chg-down"}">${floatPnl.r >= 0 ? "+" : ""}${floatPnl.r.toFixed(2)}R (${formatUsd(floatPnl.usd)})</span></div>`
            : "";
          liveHtml = `
            <div class="live-box">
              <div><strong>參考現價：</strong>${liveStr} <span class="pill ${pxInfo.failed ? "pill-fail" : "pill-live"}">${pxInfo.source}</span></div>
              <div>距止損：${formatDist(pxInfo.price, curSl)}｜距 TP1：${formatDist(pxInfo.price, curTp1)}</div>
              <div>距 TP2：${typeof curTp2 === "number" ? formatDist(pxInfo.price, curTp2) : curTp2}</div>
              ${floatTxt}
              <div class="muted">即時價格估算，僅供顯示；狀態與成績以引擎已收盤 K 線為準</div>
            </div>`;
        }

        const adds = Array.isArray(s.add_levels) && s.add_levels.length > 0
          ? s.add_levels.map((l) => `${l.price}`).join(", ")
          : "無";

        return `
          <article class="signal-card" data-sid="${s.signal_id}">
            <div class="signal-card-head">
              <div>
                <div class="signal-card-title">${s.symbol} ${dirBadge}</div>
                <div class="muted">${s.signal_id}｜策略 ${s.strategy}｜${formatTaipeiTime(s.published_at_utc)}</div>
              </div>
              <span class="pill pill-live">${lifecycleLabel(s.lifecycle_state)}</span>
            </div>
            <div class="badge badge-warn" style="align-self:flex-start;">模擬驗證中，非投資建議</div>
            <div class="signal-kv">
              <div><span class="muted">入場區間：</span>${s.entry_low} ~ ${s.entry_high}</div>
              <div><span class="muted">補倉價：</span>${adds}</div>
              <div><span class="muted">止損：</span>${curSl}</div>
              <div><span class="muted">TP1 / TP2：</span>${curTp1} / ${curTp2}</div>
              <div><span class="muted">建議風險：</span>${s.risk_pct_pool}% ($${s.risk_usd})</div>
              <div><span class="muted">補倉階段：</span>${s.stages_filled || 0}/3</div>
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
    const curTp1 = sig.current_tp1 ?? sig.tp1;
    const curTp2 = sig.current_tp2 ?? sig.tp2;

    const reasonsHtml = Array.isArray(sig.reasons)
      ? sig.reasons.map((r) => `<li>${r}</li>`).join("")
      : '<li class="muted">無</li>';

    const updatesHtml = updates.length > 0
      ? `<ul class="timeline">${updates.map((u) => `<li class="timeline-item"><strong>${formatTaipeiTime(u.ts_utc)}</strong>｜<code>${u.event_type}</code> @ ${u.price}<br>${u.description}</li>`).join("")}</ul>`
      : '<p class="muted">尚無更新事件。</p>';

    const snapHtml = snapshot
      ? `<pre style="overflow-x:auto;font-size:0.8rem;">${JSON.stringify({ kline_summary: snapshot.kline_summary, indicators: snapshot.indicators, filter_checks: snapshot.filter_checks }, null, 2)}</pre>`
      : '<p class="muted">無快照資料。</p>';

    root.innerHTML = `
      <section class="card">
        <div class="section-header">
          <div>
            <h1>${sig.signal_id}｜${sig.symbol}（${sig.direction === "long" ? "做多" : "做空"}）</h1>
            <p class="subtitle">策略 ${sig.strategy}｜狀態：<strong>${lifecycleLabel(sig.lifecycle_state)}</strong>｜發布時間 (UTC+8)：${formatTaipeiTime(sig.published_at_utc)}</p>
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
          <div>距止損 (${curSl})：${formatDist(pxInfo.price, curSl)}｜距 TP1 (${curTp1})：${formatDist(pxInfo.price, curTp1)}｜距 TP2 (${curTp2})：${typeof curTp2 === "number" ? formatDist(pxInfo.price, curTp2) : curTp2}</div>
          ${floatPnl ? `<div><strong>即時浮動損益：</strong>${floatPnl.r >= 0 ? "+" : ""}${floatPnl.r.toFixed(2)}R / ${formatUsd(floatPnl.usd)}</div>` : ""}
          <div class="muted">即時價格估算，僅供顯示；報單狀態與模擬成績一律以引擎已收盤 K 線為準</div>
        </div>
      </section>

      <section class="card">
        <h2>K 線圖與計畫點位</h2>
        <div id="signal-chart-box" class="chart-container"></div>
        <p id="chart-note" class="muted" style="margin-top:6px;">標示入場區間、補倉價、止損、TP1/TP2 與實際成交點。</p>
      </section>

      <section class="card">
        <h2>原始計畫 vs 目前狀態對照</h2>
        <div class="table-wrap">
          <table class="data-table">
            <thead><tr><th>項目</th><th>發布時原始計畫</th><th>目前狀態</th></tr></thead>
            <tbody>
              <tr><td>入場區間 / 平均成本</td><td>${sig.entry_low} ~ ${sig.entry_high}</td><td>${sig.avg_entry_price || "--"}（已進場 ${sig.stages_filled || 0} 階段）</td></tr>
              <tr><td>止損價</td><td>${sig.stop_loss}</td><td>${curSl}</td></tr>
              <tr><td>TP1 / TP2</td><td>${sig.tp1} / ${sig.tp2}</td><td>${curTp1} / ${curTp2}</td></tr>
              <tr><td>風險 / 槓桿 / 爆倉價</td><td>$${sig.risk_usd} (${sig.risk_pct_pool}%) / ${sig.leverage}x / ${sig.liquidation_price}</td><td>已實現損益 ${formatUsd(sig.realized_pnl_usd)} (${sig.r_multiple}R)</td></tr>
              <tr><td>失效條件 / 有效期限</td><td>${sig.invalidation_cond}</td><td>${sig.valid_until_utc}（出場原因：${sig.exit_reason || "--"}）</td></tr>
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
      if (typeof price === "number" && !Number.isNaN(price)) {
        series.createPriceLine({ price, color, lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title });
      }
    };
    addLine(Number(sig.entry_low), "#58a6ff", "入場低");
    addLine(Number(sig.entry_high), "#58a6ff", "入場高");
    addLine(Number(sig.current_stop_loss ?? sig.stop_loss), "#f85149", "止損");
    addLine(Number(sig.tp1), "#2ea043", "TP1");
    if (typeof sig.tp2 === "number") addLine(Number(sig.tp2), "#56d364", "TP2");
    if (Array.isArray(sig.add_levels)) {
      sig.add_levels.forEach((l) => addLine(Number(l.price), "#d29922", `補倉${l.stage}`));
    }

    if (Array.isArray(sig.stages) && sig.stages.length > 0) {
      const markers = sig.stages.map((st) => ({
        time: Math.floor(new Date(st.filled_at_utc).getTime() / 1000),
        position: sig.direction === "long" ? "belowBar" : "aboveBar",
        color: "#79c0ff",
        shape: sig.direction === "long" ? "arrowUp" : "arrowDown",
        text: `階段${st.stage} @ ${st.fill_price}`,
      }));
      series.setMarkers(markers);
    }
    chart.timeScale().fitContent();
    if (note) note.textContent = `圖表週期：${chartData.interval}（已標示計畫點位與實際成交）`;
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
    const fStrat = document.getElementById("res-filter-strategy").value;
    const fMkt = document.getElementById("res-filter-market").value;
    const fDir = document.getElementById("res-filter-direction").value;
    const fReason = document.getElementById("res-filter-reason").value;

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
            <td><a href="#/signal/${encodeURIComponent(s.signal_id)}" style="color:var(--accent);">${s.signal_id}</a></td>
            <td>策略 ${s.strategy}</td>
            <td><strong>${s.symbol}</strong></td>
            <td>${s.direction === "long" ? "做多" : "做空"}</td>
            <td class="${rCls}">${rVal.toFixed(2)}R</td>
            <td class="${rCls}">${formatUsd(s.realized_pnl_usd)}</td>
            <td>${formatDuration(s.entered_at_utc, s.closed_at_utc)}</td>
            <td>${s.exit_reason || "--"}</td>
            <td>${formatTaipeiTime(s.closed_at_utc)}</td>
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
            <td><strong>${item.symbol}</strong><br><span class="muted">${item.display_name || ""}</span></td>
            <td>${marketLabel(item.market)}</td>
            <td>${renderPriceCell(item)}</td>
            <td>${chgHtml}</td>
            <td>${item.source_used || '<span class="muted">無</span>'}</td>
            <td>${statusPill(r1m.status)}</td>
            <td>${statusPill(r15m.status)}</td>
            <td>${statusPill(r1d.status)}</td>
            <td>${statusPill(item.overall_status)} <span class="muted">${note}</span></td>
          </tr>`;
      })
      .join("");
  }

  function renderWatchToday(watchData) {
    const tbody = document.getElementById("watch-tbody");
    if (!tbody) return;
    const items = watchData && Array.isArray(watchData.items) ? watchData.items : [];
    if (items.length === 0) {
      tbody.innerHTML = '<tr><td colspan="5" class="muted center">尚無觀望紀錄（等待下次掃描）</td></tr>';
      return;
    }
    tbody.innerHTML = items
      .map((w) => {
        const ind = w.indicators || {};
        const adxTxt = ind.adx_1h !== null && ind.adx_1h !== undefined ? ind.adx_1h : "--";
        const rsiTxt = ind.rsi_14 !== null && ind.rsi_14 !== undefined ? ind.rsi_14 : "--";
        const atrTxt = ind.atr_ratio !== null && ind.atr_ratio !== undefined ? ind.atr_ratio : "--";
        const badge = w.decision === "rejected" ? '<span class="pill pill-fail">風控擋下</span>' : '<span class="pill pill-warn">觀望</span>';
        return `
          <tr>
            <td><strong>${w.symbol}</strong></td>
            <td>${marketLabel(w.market)}</td>
            <td>${badge}</td>
            <td>ADX ${adxTxt} / RSI ${rsiTxt} / ATR ${atrTxt}x</td>
            <td>${w.reason || ""}</td>
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
    summaryEl.textContent = `實測時間 (UTC+8)：${formatTaipeiTime(probeData.generated_at_utc)}，共 ${probeData.results.length} 組測試。`;
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
      sumEl.textContent = `近 24 小時排程執行次數：${health.runs_last_24h} 次｜自動排程時間：每小時 07, 22, 37, 52 分 UTC`;
    }
    if (!Array.isArray(runLog) || runLog.length === 0) {
      box.textContent = "尚無排程紀錄。";
      return;
    }
    const items = runLog.slice(0, 10).map((r) => {
      const cls = r.status === "ok" ? "pill-ok" : "pill-fail";
      const sigInfo = r.signals_published !== undefined ? `｜報單 ${r.signals_published}、觀望 ${r.watch_count ?? 0}` : "";
      return `<li>${formatTaipeiTime(r.run_at_utc)} — <span class="pill ${cls}">${r.status}</span>（${r.instruments_ok}/${r.instruments_total} 正常${sigInfo}，耗時 ${r.duration_sec}s）</li>`;
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
    const delayBanner = document.getElementById("delay-banner");
    if (health && health.updated_at_utc) {
      lastUpdatedEl.textContent = formatTaipeiTime(health.updated_at_utc);
      const ageMin = (Date.now() - new Date(health.updated_at_utc).getTime()) / 60000;
      if (ageMin > STALE_MINUTES) {
        schedLight.className = "light light-warn";
        schedLight.textContent = `排程：延遲 (${Math.round(ageMin)}m)`;
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

    if (!cachedBacktest || !Array.isArray(cachedBacktest.results) || cachedBacktest.results.length === 0) {
      metaEl.textContent = "目前尚無策略 B 回測結果（等待每週自動排程或手動執行 Strategy B Weekly Backtest）。";
      tbody.innerHTML = '<tr><td colspan="7" class="muted center">目前尚無策略 B 回測結果。</td></tr>';
      return;
    }

    const s = cachedBacktest.summary || {};
    metaEl.textContent = `最新回測時間 (UTC+8)：${formatTaipeiTime(cachedBacktest.generated_at_utc)}｜總計 ${s.total ?? cachedBacktest.results.length} 檔｜通過 ${s.passed ?? 0}｜未通過 ${s.failed ?? 0}｜歷史不足 ${s.insufficient_history ?? 0}（歷史不代表未來）`;

    tbody.innerHTML = cachedBacktest.results
      .map((item) => {
        let statusBadge = '<span class="pill pill-fail">未通過</span>';
        if (item.status === "insufficient_history") {
          statusBadge = '<span class="pill pill-warn">歷史不足 (&lt;5年)</span>';
        } else if (item.status === "data_failed") {
          statusBadge = '<span class="pill pill-fail">抓取失敗</span>';
        } else if (item.passed) {
          statusBadge = '<span class="pill pill-ok">通過驗證</span>';
        }

        const paramsText = item.chosen_params && Object.keys(item.chosen_params).length > 0
          ? Object.entries(item.chosen_params).map(([k, v]) => `${k}=${v}`).join(", ")
          : "--";
        const stratText = item.chosen_strategy ? `${escapeHtml(item.chosen_strategy)} (${escapeHtml(paramsText)})` : "--";
        const yrsText = item.history_years !== undefined && item.history_years !== null
          ? `${Number(item.history_years).toFixed(2)} 年 (${item.bar_count || 0} 根)`
          : "--";

        return `
          <tr>
            <td><strong>${escapeHtml(item.symbol)}</strong><br><span class="muted">${escapeHtml(item.market || "")}</span></td>
            <td>${yrsText}</td>
            <td>${statusBadge}</td>
            <td><code>${stratText}</code></td>
            <td>${formatMetricSummary(item.in_sample)}</td>
            <td>${formatMetricSummary(item.out_of_sample)}</td>
            <td>${escapeHtml(item.reason || "--")}</td>
          </tr>
        `;
      })
      .join("");
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
    } else if (route === "data") {
      document.getElementById("view-data").classList.remove("hidden");
      document.querySelector('.nav-link[data-route="data"]')?.classList.add("active");
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
    if ((window.location.hash || "#/wall").startsWith("#/wall") || !window.location.hash) renderWallCards();
  }

  async function refreshAll() {
    const errBox = document.getElementById("fetch-error-box");
    try {
      const [instRes, healthRes, controlRes, runLogRes, probeRes, signalsRes, watchRes, btRes] = await Promise.allSettled([
        fetchDataJson("instruments.json"),
        fetchDataJson("data_health.json"),
        fetchDataJson("control_status.json"),
        fetchDataJson("run_log.json"),
        fetchDataJson("data_probe_report.json"),
        fetchDataJson("signals_index.json"),
        fetchDataJson("watch_today.json"),
        fetchDataJson("backtest_results.json"),
      ]);

      if (instRes.status === "rejected") {
        throw new Error(`無法讀取 data 分支資料（${instRes.reason.message}）。請確認已於 GitHub Actions 執行過 Market Scan。`);
      }

      errBox.classList.add("hidden");
      cachedInstruments = instRes.value.instruments || [];
      cachedSignals = signalsRes.status === "fulfilled" && Array.isArray(signalsRes.value) ? signalsRes.value : [];
      cachedWatch = watchRes.status === "fulfilled" ? watchRes.value : null;
      cachedBacktest = btRes.status === "fulfilled" ? btRes.value : null;

      renderInstruments();
      renderWallCards();
      renderResultsTable();
      renderBacktestResults();

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
      if (cachedWatch) renderWatchToday(cachedWatch);
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
    document.getElementById(id).addEventListener("change", renderResultsTable);
  });

  document.getElementById("refresh-btn").addEventListener("click", () => {
    refreshAll();
    refreshLivePrices();
  });

  window.addEventListener("hashchange", applyRoute);

  applyRoute();
  refreshAll();
  refreshLivePrices();
  setInterval(refreshAll, DATA_REFRESH_MS);
  setInterval(refreshLivePrices, LIVE_REFRESH_MS);
})();
