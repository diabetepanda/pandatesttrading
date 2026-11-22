(function (global) {
  "use strict";

  function escHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function fmtSignedNum(val, decimals, unit) {
    if (val === null || val === undefined || Number.isNaN(Number(val))) return "--";
    const num = Number(val);
    const sign = num > 0 ? "+" : "";
    const formatted = decimals === 0
      ? Math.round(num).toLocaleString("en-US")
      : num.toLocaleString("en-US", { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
    return `${sign}${formatted}${unit ? ` ${unit}` : ""}`;
  }

  function fmtZ(val) {
    if (val === null || val === undefined || Number.isNaN(Number(val))) return "--";
    const num = Number(val);
    const sign = num > 0 ? "+" : "";
    return `${sign}${num.toFixed(2)}`;
  }

  async function fetchFlowTwJson(dataBase, timeoutMs) {
    const base = dataBase || (global.SIGNAL_DESK_CONFIG && global.SIGNAL_DESK_CONFIG.DATA_BASE) || "";
    const ms = timeoutMs || (global.SIGNAL_DESK_CONFIG && global.SIGNAL_DESK_CONFIG.REQUEST_TIMEOUT_MS) || 6000;

    async function _get(url) {
      const controller = typeof AbortController !== "undefined" ? new AbortController() : null;
      const timer = setTimeout(() => {
        if (controller) controller.abort();
      }, ms);
      try {
        const resp = await fetch(url, controller ? { cache: "no-store", signal: controller.signal } : { cache: "no-store" });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        return await resp.json();
      } finally {
        clearTimeout(timer);
      }
    }

    if (base) {
      try {
        const remote = await _get(`${base}/flow_tw.json?t=${Date.now()}`);
        if (remote && (remote.a1 || remote.a2)) return remote;
      } catch (_err) {
        // fallback to local data/flow_tw.json
      }
    }
    return _get(`./data/flow_tw.json?t=${Date.now()}`);
  }

  function renderFlowTwCardHtml(doc, options) {
    const opts = options || {};
    const researchHref = opts.researchHref || "#/research/flow_signals";
    const compact = Boolean(opts.compact);

    if (!doc || (!doc.a1 && !doc.a2)) {
      return `
        <section class="card flow-tw-card" aria-label="台股隔天風險提示">
          <div class="flow-tw-head">
            <div>
              <h2>台股隔天風險提示（^TWII）</h2>
              <p class="subtitle">資料載入中...</p>
            </div>
          </div>
          <div class="flow-tw-footer">
            <span class="flow-tw-disclaimer">只看隔天、證據偏弱，僅供參考，不是買賣訊號</span>
            <a href="${escHtml(researchHref)}" class="flow-tw-research-link">查看深度研究（2026-11）→</a>
          </div>
        </section>
      `;
    }

    const a1 = doc.a1 || {};
    const a2 = doc.a2 || {};
    const isStale = Boolean(doc.stale || doc.data_status === "資料過期");
    const isSettle = Boolean(doc.is_settlement_day || a2.is_settlement_day);
    const a1Trig = Boolean(a1.triggered);
    const a2Trig = Boolean(a2.triggered);
    const a2ExcludedBySettle = Boolean(a2.excluded_by_settlement);

    const a1ZStr = fmtZ(a1.z60);
    const a2ZStr = fmtZ(a2.z60);
    const a1ValStr = fmtSignedNum(a1.value, 2, "億元");
    const a2ChgStr = fmtSignedNum(a2.value, 0, "口");
    const a2NetStr = a2.net_oi != null ? `${Number(a2.net_oi).toLocaleString("en-US")} 口` : "--";

    const alerts = [];
    if (a1Trig) {
      alerts.push({
        cls: "is-bull",
        badge: "A1 偏多提示",
        text: "外資異常大買，隔天偏多（歷史上漲機率約 73%，平常約 57%）",
      });
    }
    if (a2Trig) {
      alerts.push({
        cls: "is-bear",
        badge: "A2 風險提示",
        text: "外資期貨空單暴增，隔天下跌風險升高（歷史下跌機率約 68%）",
      });
    }
    if (!a1Trig && !a2Trig) {
      const settleExtra = a2ExcludedBySettle
        ? "｜今日為台指期結算日，已排除 A2 期貨單日變化警示"
        : "";
      alerts.push({
        cls: "is-neut",
        badge: "狀態正常",
        text: `今日無異常（外資買賣超 z-score：${a1ZStr}｜台指期淨未平倉單日變化 z-score：${a2ZStr}${settleExtra}）`,
      });
    }

    let statusBadgeHtml = '<span class="badge badge-ok">資料正常</span>';
    if (isStale) {
      statusBadgeHtml = '<span class="badge badge-danger">資料過期</span>';
    } else if (a1Trig || a2Trig) {
      statusBadgeHtml = '<span class="badge badge-warn">今日有隔天提示</span>';
    }

    const settleBadgeHtml = isSettle
      ? '<span class="badge badge-warn">台指期結算日（排除 A2）</span>'
      : "";

    const hist = Array.isArray(doc.history_60d) ? doc.history_60d : [];
    const a1Count60 = hist.filter((r) => r && r.a1_triggered).length;
    const a2Count60 = hist.filter((r) => r && r.a2_triggered).length;
    const dateStr = doc.latest_date || "--";

    const alertsHtml = alerts
      .map(
        (al) => `
        <div class="flow-tw-alert ${al.cls}">
          <span class="flow-tw-alert-badge">${escHtml(al.badge)}</span>
          <span class="flow-tw-alert-text">${escHtml(al.text)}</span>
        </div>
      `
      )
      .join("");

    const cardTag = compact ? "div" : "section";
    const cardCls = compact ? "flow-tw-card is-compact" : "card flow-tw-card";

    return `
      <${cardTag} class="${cardCls}" aria-label="台股隔天風險提示">
        <div class="flow-tw-head">
          <div>
            <h2 class="flow-tw-title">台股隔天風險提示（^TWII）</h2>
            <p class="subtitle flow-tw-sub">資料日期：<strong>${escHtml(dateStr)}</strong>｜近 60 交易日觸發：A1 偏多 ${a1Count60} 次、A2 避險 ${a2Count60} 次</p>
          </div>
          <div class="flow-tw-badges">
            ${statusBadgeHtml}
            ${settleBadgeHtml}
          </div>
        </div>

        ${isStale && doc.stale_reason ? `<div class="flow-tw-stale-note">⚠️ 資料過期（暫保留前次資料）：${escHtml(doc.stale_reason)}</div>` : ""}

        <div class="flow-tw-alerts">
          ${alertsHtml}
        </div>

        <div class="flow-tw-metrics-grid">
          <div class="flow-tw-metric-box${a1Trig ? " is-triggered-bull" : ""}">
            <div class="flow-tw-metric-top">
              <span class="flow-tw-metric-name">A1 外資買賣超 z-score</span>
              <span class="flow-tw-metric-th">門檻 z &gt; +1.5</span>
            </div>
            <div class="flow-tw-metric-vals">
              <strong class="flow-tw-z">${escHtml(a1ZStr)}</strong>
              <span class="flow-tw-raw">當日買賣超：${escHtml(a1ValStr)}</span>
            </div>
          </div>

          <div class="flow-tw-metric-box${a2Trig ? " is-triggered-bear" : ""}">
            <div class="flow-tw-metric-top">
              <span class="flow-tw-metric-name">A2 台指期淨未平倉（單日變化）</span>
              <span class="flow-tw-metric-th">門檻 z &lt; -1.5（排除結算日）</span>
            </div>
            <div class="flow-tw-metric-vals">
              <strong class="flow-tw-z">${escHtml(a2ZStr)}</strong>
              <span class="flow-tw-raw">單日變化：${escHtml(a2ChgStr)}｜總淨未平倉：${escHtml(a2NetStr)}</span>
            </div>
          </div>
        </div>

        <div class="flow-tw-footer">
          <span class="flow-tw-disclaimer">只看隔天、證據偏弱，僅供參考，不是買賣訊號</span>
          <a href="${escHtml(researchHref)}" class="flow-tw-research-link">查看深度研究：大戶／法人資金流回測（2026-11）→</a>
        </div>
      </${cardTag}>
    `;
  }

  function scanFlowTwGlossary(containerEl) {
    const g = global.SIGNAL_DESK_GLOSSARY;
    if (!g || typeof g.scanContainers !== "function" || !g.isEnabled() || !containerEl) return;
    g.scanContainers(containerEl, ".flow-tw-card");
  }

  function mountFlowTwCard(slotId, doc, options) {
    if (typeof document === "undefined") return;
    const el = document.getElementById(slotId);
    if (!el) return;
    el.innerHTML = renderFlowTwCardHtml(doc, options);
    scanFlowTwGlossary(el);
  }

  global.SIGNAL_DESK_FLOW_TW = {
    fetchFlowTwJson,
    renderFlowTwCardHtml,
    mountFlowTwCard,
    scanFlowTwGlossary,
  };
})(typeof window !== "undefined" ? window : globalThis);
