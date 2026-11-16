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
  let cachedInstruments = [];
  let livePrices = {}; // symbol -> { price, changePct, source } | { failed: true }

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

  function marketLabel(market) {
    const map = { crypto: "加密貨幣", forex: "外匯", commodity: "商品", stock: "股票" };
    return map[market] || market;
  }

  async function fetchWithTimeout(url) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    try {
      const resp = await fetch(url, { cache: "no-store", signal: controller.signal });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      return await resp.json();
    } finally {
      clearTimeout(timer);
    }
  }

  async function fetchDataJson(filename) {
    return fetchWithTimeout(`${DATA_BASE}/${filename}?t=${Date.now()}`);
  }

  function isLiveCapable(symbol) {
    return Boolean(
      LIVE_CRYPTO_SYMBOLS[symbol] || LIVE_METAL_SYMBOLS[symbol] || LIVE_TRADINGVIEW_SYMBOLS[symbol]
    );
  }

  async function fetchLiveCrypto() {
    const results = {};
    const pending = new Set(Object.keys(LIVE_CRYPTO_SYMBOLS));

    // 1. 優先 OKX
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
    } catch (_) {
      // Fallback to Binance Vision
    }

    // 2. 備援 Binance data-api.binance.vision
    if (pending.size > 0) {
      try {
        const binSymbols = Array.from(pending)
          .map((s) => LIVE_CRYPTO_SYMBOLS[s].binance)
          .filter(Boolean);
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
      } catch (_) {
        // Fallback to Kraken
      }
    }

    // 3. 最後備援 Kraken
    if (pending.size > 0) {
      try {
        const krakenPairs = Array.from(pending)
          .map((s) => LIVE_CRYPTO_SYMBOLS[s].kraken)
          .filter(Boolean);
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
      } catch (_) {
        // All failed for remaining symbols
      }
    }

    for (const sym of pending) {
      results[sym] = { failed: true };
    }
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
          // 金屬：gold-api 為主來源，TradingView 為備援並補充 24h 漲跌幅
          const [goldRes, tvRes] = await Promise.allSettled([
            fetchWithTimeout(`https://api.gold-api.com/price/${metalCode}`),
            fetchTradingViewSymbol(tvCode),
          ]);
          const tvOk = tvRes.status === "fulfilled" ? tvRes.value : null;
          if (
            goldRes.status === "fulfilled" &&
            goldRes.value &&
            goldRes.value.price !== undefined &&
            !Number.isNaN(Number(goldRes.value.price))
          ) {
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

        // 外匯與能源：TradingView 為主來源
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

    if (cachedInstruments.length > 0) {
      renderInstruments();
    }
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

    if (!schedStr) {
      return '<span class="pill pill-fail">資料來源失敗</span>';
    }
    return `${schedStr} <span class="muted">(僅排程)</span>`;
  }

  function renderInstruments() {
    const tbody = document.getElementById("instruments-tbody");
    const rows = cachedInstruments.filter(
      (item) => currentMarketFilter === "all" || item.market === currentMarketFilter
    );

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
          </tr>
        `;
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
          .map((a) => {
            const cls = a.ok ? "pill-ok" : "pill-fail";
            const label = a.ok ? `${a.adapter}: OK (${a.bar_count}根, ${a.latency_ms}ms)` : `${a.adapter}: 失敗`;
            return `<span class="pill ${cls}" title="${a.error || ""}">${label}</span>`;
          })
          .join(" ");
        const missPct = row.missing_ratio !== undefined ? `${(row.missing_ratio * 100).toFixed(1)}%` : "--";
        const delayTxt = row.delay_minutes !== undefined && row.delay_minutes !== null ? `${row.delay_minutes}m` : "--";
        const reasonTxt = row.quality_reason ? `<br><span class="muted">缺K:${missPct} 延遲:${delayTxt}｜${row.quality_reason}</span>` : "";
        return `
          <tr>
            <td><strong>${row.symbol}</strong></td>
            <td>${row.interval}</td>
            <td>${statusPill(row.quality_status)}${reasonTxt}</td>
            <td>${attemptsHtml || '<span class="muted">無</span>'}</td>
          </tr>
        `;
      })
      .join("");
  }

  function renderRunLog(runLog) {
    const box = document.getElementById("run-log-box");
    if (!Array.isArray(runLog) || runLog.length === 0) {
      box.textContent = "尚無排程紀錄。";
      return;
    }
    const items = runLog.slice(0, 10).map((r) => {
      const cls = r.status === "ok" ? "pill-ok" : "pill-fail";
      return `<li>${formatTaipeiTime(r.run_at_utc)} — <span class="pill ${cls}">${r.status}</span>（${r.instruments_ok}/${r.instruments_total} 正常，耗時 ${r.duration_sec}s）</li>`;
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

  async function refreshAll() {
    const errBox = document.getElementById("fetch-error-box");
    try {
      const [instRes, healthRes, controlRes, runLogRes, probeRes] = await Promise.allSettled([
        fetchDataJson("instruments.json"),
        fetchDataJson("data_health.json"),
        fetchDataJson("control_status.json"),
        fetchDataJson("run_log.json"),
        fetchDataJson("data_probe_report.json"),
      ]);

      if (instRes.status === "rejected") {
        throw new Error(`無法讀取 data 分支資料（${instRes.reason.message}）。請確認已於 GitHub Actions 執行過 Market Scan。`);
      }

      errBox.classList.add("hidden");
      cachedInstruments = instRes.value.instruments || [];
      renderInstruments();

      const health = healthRes.status === "fulfilled" ? healthRes.value : null;
      const control = controlRes.status === "fulfilled" ? controlRes.value : null;
      updateLights(health, control);

      if (runLogRes.status === "fulfilled") {
        renderRunLog(runLogRes.value);
      }
      if (probeRes.status === "fulfilled") {
        renderProbe(probeRes.value);
      }
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

  document.getElementById("refresh-btn").addEventListener("click", () => {
    refreshAll();
    refreshLivePrices();
  });

  refreshAll();
  refreshLivePrices();
  setInterval(refreshAll, DATA_REFRESH_MS);
  setInterval(refreshLivePrices, LIVE_REFRESH_MS);
})();
