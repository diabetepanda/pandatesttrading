(function () {
  "use strict";

  const DATA_BASE = "https://raw.githubusercontent.com/diabetepanda/pandatesttrading/data";
  const STALE_MINUTES = 45;
  const REFRESH_MS = 60000;

  let currentMarketFilter = "all";
  let cachedInstruments = [];

  function formatTaipeiTime(isoUtc) {
    if (!isoUtc) return "--";
    const dt = new Date(isoUtc);
    if (Number.isNaN(dt.getTime())) return isoUtc;
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

  function formatPrice(val) {
    if (val === null || val === undefined || Number.isNaN(Number(val))) {
      return '<span class="pill pill-fail">資料來源失敗</span>';
    }
    const num = Number(val);
    if (num >= 1000) return num.toLocaleString("en-US", { maximumFractionDigits: 2 });
    if (num >= 1) return num.toLocaleString("en-US", { maximumFractionDigits: 4 });
    return num.toLocaleString("en-US", { maximumFractionDigits: 6 });
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

  async function fetchDataJson(filename) {
    const url = `${DATA_BASE}/${filename}?t=${Date.now()}`;
    const resp = await fetch(url, { cache: "no-store" });
    if (!resp.ok) {
      throw new Error(`${filename} HTTP ${resp.status}`);
    }
    return resp.json();
  }

  function renderInstruments() {
    const tbody = document.getElementById("instruments-tbody");
    const rows = cachedInstruments.filter(
      (item) => currentMarketFilter === "all" || item.market === currentMarketFilter
    );

    if (rows.length === 0) {
      tbody.innerHTML = '<tr><td colspan="8" class="muted center">無符合條件的標的資料</td></tr>';
      return;
    }

    tbody.innerHTML = rows
      .map((item) => {
        const iv = item.intervals || {};
        const r1m = iv["1m"] || {};
        const r15m = iv["15m"] || {};
        const r1d = iv["1d"] || {};
        const note = r15m.reason || r1d.reason || "";
        return `
          <tr>
            <td><strong>${item.symbol}</strong><br><span class="muted">${item.display_name || ""}</span></td>
            <td>${marketLabel(item.market)}</td>
            <td>${formatPrice(item.latest_price)}</td>
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
        '<tr><td colspan="8" class="center"><span class="pill pill-fail">資料來源失敗</span></td></tr>';
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

  document.getElementById("refresh-btn").addEventListener("click", refreshAll);

  refreshAll();
  setInterval(refreshAll, REFRESH_MS);
})();
