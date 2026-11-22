(function () {
  "use strict";

  const CFG = window.SIGNAL_DESK_CONFIG || {};
  const DATA_BASE = CFG.DATA_BASE || "https://raw.githubusercontent.com/diabetepanda/pandatesttrading/data";
  const REQUEST_TIMEOUT_MS = CFG.REQUEST_TIMEOUT_MS || 8000;

  let cachedMacro = null;
  let activeTab = "overview";
  let calRange = "week2"; // 預設：本週與下週
  let calStars = "3"; // 預設：僅顯示 ★★★
  let calIndicator = "all";
  let calPastMonth = "recent7"; // 預設：最近 7 天
  let expandedPastIds = new Set();
  let lastFocusEl = null;
  let activeAssetImpactIdx = null;

  const LS_STANCE_OPEN = "macro_overview_stance_open";
  const LS_FEDWATCH_OPEN = "macro_fedwatch_open";
  const LS_LOGIC_OPEN = "macro_logic_fold_open";

  function getBoolPref(key, fallback) {
    try {
      const v = window.localStorage ? window.localStorage.getItem(key) : null;
      if (v === "1") return true;
      if (v === "0") return false;
    } catch (_e) {
      // ignore
    }
    return !!fallback;
  }

  function setBoolPref(key, flag) {
    try {
      if (window.localStorage) window.localStorage.setItem(key, flag ? "1" : "0");
    } catch (_e) {
      // ignore
    }
  }

  const INDICATOR_FILTER_GROUPS = {
    FED_RATE: ["FED_RATE"],
    CPI: ["CPI", "CORE_CPI"],
    PCE: ["PCE", "CORE_PCE"],
    PPI: ["PPI"],
    NFP: ["NFP", "UNRATE", "AVG_HOURLY_EARNINGS"],
    ICSA: ["ICSA"],
    JOLTS: ["JOLTS"],
    GDP: ["GDP"],
    ISM_MFG: ["ISM_MFG"],
    ISM_SVC: ["ISM_SVC"],
    RETAIL_SALES: ["RETAIL_SALES"],
  };

  function escHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function escAttr(s) {
    return escHtml(s).replace(/'/g, "&#39;");
  }

  function formatPctChip(label, val) {
    if (val === null || val === undefined || Number.isNaN(Number(val))) {
      return `<span class="macro-rchip na">${escHtml(label)} —</span>`;
    }
    const num = Number(val);
    const cls = num > 0 ? "up" : num < 0 ? "dn" : "na";
    const sign = num > 0 ? "+" : "";
    return `<span class="macro-rchip ${cls}">${escHtml(label)} ${sign}${num.toFixed(2)}%</span>`;
  }

  function formatPctCell(val) {
    if (val === null || val === undefined || Number.isNaN(Number(val))) {
      return '<td class="muted">—</td>';
    }
    const num = Number(val);
    const cls = num > 0 ? "chg-up" : num < 0 ? "chg-down" : "muted";
    const sign = num > 0 ? "+" : "";
    return `<td class="${cls}">${sign}${num.toFixed(2)}%</td>`;
  }

  function formatCountdown(targetIso, nowMs) {
    if (!targetIso) return { text: "--", done: true };
    const t = new Date(targetIso).getTime();
    if (!Number.isFinite(t)) return { text: "--", done: true };
    const ms = t - (nowMs != null ? nowMs : Date.now());
    if (ms <= 0) return { text: "已到公布時間 · 待更新", done: true };
    const s = Math.floor(ms / 1000);
    const d = Math.floor(s / 86400);
    const hh = String(Math.floor((s % 86400) / 3600)).padStart(2, "0");
    const mm = String(Math.floor((s % 3600) / 60)).padStart(2, "0");
    const ss = String(s % 60).padStart(2, "0");
    if (d > 0) return { text: `倒數 ${d} 天 ${hh}:${mm}:${ss}`, done: false };
    return { text: `倒數 ${hh}:${mm}:${ss}`, done: false };
  }

  function tickCountdowns() {
    const nowMs = Date.now();
    document.querySelectorAll("[data-cd]").forEach((el) => {
      const res = formatCountdown(el.getAttribute("data-cd"), nowMs);
      el.textContent = res.text;
      el.classList.toggle("done", res.done);
    });
  }

  function getTaipeiDateParts(ms) {
    // 台北時間固定 UTC+8，無夏令時間，可直接平移 +8h 用 UTC getters 取得台北年月日時分與星期
    const shifted = new Date(ms + 8 * 3600 * 1000);
    return {
      year: shifted.getUTCFullYear(),
      month: shifted.getUTCMonth() + 1,
      day: shifted.getUTCDate(),
      weekdayMon0: (shifted.getUTCDay() + 6) % 7, // Mon=0 .. Sun=6
      ym: `${shifted.getUTCFullYear()}-${String(shifted.getUTCMonth() + 1).padStart(2, "0")}`,
    };
  }

  function getTaipeiWeekBounds(nowMs) {
    const cur = getTaipeiDateParts(nowMs);
    // 台北本週一 00:00:00+08:00 對應的 UTC ms
    const startOfTodayUtc = Date.UTC(cur.year, cur.month - 1, cur.day) - 8 * 3600 * 1000;
    const thisWeekStartMs = startOfTodayUtc - cur.weekdayMon0 * 86400 * 1000;
    const thisWeekEndMs = thisWeekStartMs + 7 * 86400 * 1000 - 1;
    const nextWeekStartMs = thisWeekStartMs + 7 * 86400 * 1000;
    const nextWeekEndMs = thisWeekStartMs + 14 * 86400 * 1000 - 1;
    return { thisWeekStartMs, thisWeekEndMs, nextWeekStartMs, nextWeekEndMs };
  }

  function matchesIndicatorFilter(itemIndicatorId, filterVal) {
    if (!filterVal || filterVal === "all") return true;
    const allowed = INDICATOR_FILTER_GROUPS[filterVal] || [filterVal];
    return allowed.includes(itemIndicatorId);
  }

  function filterUpcomingEvents(events, options) {
    const opts = options || {};
    const nowMs = opts.nowMs != null ? opts.nowMs : Date.now();
    const range = opts.range || calRange;
    const stars = opts.stars != null ? String(opts.stars) : calStars;
    const indicator = opts.indicator || calIndicator;
    const bounds = getTaipeiWeekBounds(nowMs);
    const max60dMs = nowMs + 60 * 86400 * 1000;

    return (events || []).filter((ev) => {
      const evMs = new Date(ev.time_utc || ev.time_taipei_iso).getTime();
      if (!Number.isFinite(evMs) || evMs <= nowMs) return false;
      if (stars === "3" && Number(ev.stars || 0) < 3) return false;
      if (!matchesIndicatorFilter(ev.indicator_id, indicator)) return false;

      if (range === "this_week") {
        return evMs >= bounds.thisWeekStartMs && evMs <= bounds.thisWeekEndMs;
      }
      if (range === "next_week") {
        return evMs >= bounds.nextWeekStartMs && evMs <= bounds.nextWeekEndMs;
      }
      if (range === "week2") {
        return evMs >= bounds.thisWeekStartMs && evMs <= bounds.nextWeekEndMs;
      }
      if (range === "60d") {
        return evMs <= max60dMs;
      }
      return true;
    });
  }

  function getAvailablePastMonths(pastEvents, nowMs) {
    const refMs = nowMs != null ? nowMs : Date.now();
    const cur = getTaipeiDateParts(refMs);
    const validMonths = new Set();
    // 最多往前 12 個月
    for (let i = 0; i < 12; i++) {
      let y = cur.year;
      let m = cur.month - i;
      while (m <= 0) {
        m += 12;
        y -= 1;
      }
      validMonths.add(`${y}-${String(m).padStart(2, "0")}`);
    }
    const found = new Set();
    (pastEvents || []).forEach((ev) => {
      const ym = String(ev.time_taipei || "").slice(0, 7);
      if (validMonths.has(ym)) found.add(ym);
    });
    return Array.from(found).sort().reverse();
  }

  function filterPastEvents(events, options) {
    const opts = options || {};
    const nowMs = opts.nowMs != null ? opts.nowMs : Date.now();
    const pastMonth = opts.pastMonth || calPastMonth;
    const stars = opts.stars != null ? String(opts.stars) : calStars;
    const indicator = opts.indicator || calIndicator;
    const sevenDaysMs = 7 * 86400 * 1000;

    return (events || []).filter((ev) => {
      const evMs = new Date(ev.time_utc || ev.time_taipei_iso).getTime();
      if (!Number.isFinite(evMs) || evMs > nowMs) return false;
      if (stars === "3" && Number(ev.stars || 0) < 3) return false;
      if (!matchesIndicatorFilter(ev.indicator_id, indicator)) return false;

      if (pastMonth === "recent7") {
        return evMs >= nowMs - sevenDaysMs;
      }
      return String(ev.time_taipei || "").slice(0, 7) === pastMonth;
    });
  }

  function buildSparklineSvg(primary, secondary) {
    if (!Array.isArray(primary) || primary.length < 2) {
      return '<div class="muted" style="font-size:0.76rem;">暫無走勢資料</div>';
    }
    const w = 320;
    const h = 54;
    const pad = 5;
    const all = Array.isArray(secondary) && secondary.length ? primary.concat(secondary) : primary.slice();
    let min = Math.min.apply(null, all);
    let max = Math.max.apply(null, all);
    if (min > 0 && min < 1.5 && max < 3.0) min = 0; // 月環比圖保留 0 軸基準
    if (max < 0) max = 0;
    const span = max - min || 1;
    const toPts = (arr) =>
      arr.map((v, i) => {
        const x = pad + ((w - 2 * pad) * i) / (arr.length - 1);
        const y = pad + (h - 2 * pad) * (1 - (v - min) / span);
        return [Math.round(x * 10) / 10, Math.round(y * 10) / 10];
      });

    let inner = "";
    if (min < 0 && max > 0) {
      const zy = Math.round((pad + (h - 2 * pad) * (1 - (0 - min) / span)) * 10) / 10;
      inner += `<line x1="0" y1="${zy}" x2="${w}" y2="${zy}" stroke="#30363d" stroke-width="1" stroke-dasharray="3 3"/>`;
    }
    const pPts = toPts(primary);
    const pPath = pPts.map((p, i) => `${i === 0 ? "M" : "L"}${p[0]} ${p[1]}`).join(" ");
    inner += `<path d="${pPath}" fill="none" stroke="#58a6ff" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;
    if (Array.isArray(secondary) && secondary.length === primary.length) {
      const sPts = toPts(secondary);
      const sPath = sPts.map((p, i) => `${i === 0 ? "M" : "L"}${p[0]} ${p[1]}`).join(" ");
      inner += `<path d="${sPath}" fill="none" stroke="#8b949e" stroke-width="1.6" stroke-dasharray="4 3" stroke-linejoin="round"/>`;
    }
    const last = pPts[pPts.length - 1];
    inner += `<circle cx="${last[0]}" cy="${last[1]}" r="2.8" fill="#58a6ff"/>`;
    return `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true">${inner}</svg>`;
  }

  function renderIndicatorCardHtml(c) {
    const chgCls = c.change_dir === "up" ? "chg-up" : c.change_dir === "down" ? "chg-down" : "muted";
    const updHtml = c.updating
      ? `<div class="macro-updating-pill" title="${escAttr(c.updating_notice || "")}">⚠ 資料更新中（保留前次資料）</div>`
      : "";
    const consTxt = c.consensus_str ? escHtml(c.consensus_str) : '<span class="muted">無可靠預期值</span>';
    const surpTxt = c.surprise ? escHtml(c.surprise) : "—";
    const surpBasisNote = c.surprise_basis === "prev" ? "（依前值比較）" : c.consensus_source ? `（${escHtml(c.consensus_source)}）` : "";
    const nextHtml = c.next_release
      ? `<div class="macro-ind-next"><span>下次公布：<strong>${escHtml(c.next_release.release_taipei)}</strong></span><span class="macro-cd" data-cd="${escAttr(c.next_release.release_taipei_iso)}">${escHtml(formatCountdown(c.next_release.release_taipei_iso).text)}</span></div>`
      : `<div class="macro-ind-next"><span>更新頻率：每日美債交易日收盤更新</span></div>`;

    return `
      <article class="macro-ind-card" data-indicator-id="${escAttr(c.id)}">
        <div class="macro-ind-head">
          <div>
            <div class="macro-ind-name">${escHtml(c.name)}</div>
            <div class="macro-ind-meta">${escHtml(c.period)} · <code>${escHtml(c.code)}</code></div>
            ${updHtml}
          </div>
          <span class="macro-pill-tag ${escAttr(c.verdict || "neut")}">${escHtml(c.verdict_text || "🟡 中性")}</span>
        </div>
        <div class="macro-ind-val macro-num">${escHtml(c.value_str)}${c.unit ? ` <span class="macro-ind-unit">${escHtml(c.unit)}</span>` : ""}</div>
        <div class="macro-ind-sub">${escHtml(c.sub_info || "")}</div>
        <div class="macro-ind-kv">
          <div><span class="k">前值</span><span class="v macro-num">${escHtml(c.prev_str || "--")}</span></div>
          <div><span class="k">較前值變化</span><span class="v macro-num ${chgCls}">${escHtml(c.change_str || "—")}</span></div>
          <div><span class="k">公布日期（台北）</span><span class="v macro-num">${escHtml(c.release_date_taipei || "--")}</span></div>
          <div><span class="k">市場預期</span><span class="v macro-num">${consTxt}</span></div>
        </div>
        <div class="macro-ind-surp">比較結果：<strong>${surpTxt}</strong> <span class="muted">${surpBasisNote}</span></div>
        <div class="macro-spark-box">
          ${buildSparklineSvg(c.chart_values, c.chart_values_secondary)}
          <div class="macro-spark-cap"><span>${escHtml(c.chart_label || "")}</span><span>最新 ${escHtml(c.value_str)}</span></div>
        </div>
        ${nextHtml}
        <p class="macro-ind-why">${escHtml(c.why || "")}</p>
        <button type="button" class="macro-hist-open" data-indicator-id="${escAttr(c.id)}">
          <span>📊 過去公布後市場怎麼走</span>
          <span aria-hidden="true">›</span>
        </button>
        <div class="macro-ind-src" title="${escAttr(c.source || "")}">來源：${escHtml(c.source_used || c.source || "")}</div>
      </article>
    `;
  }

  function renderStatsGroupHtml(groups, introNote) {
    if (!groups || !groups.length) return "";
    const cards = groups
      .map((g) => {
        const lines = ["NAS100", "SP500", "TWII", "BTCUSDT"]
          .map((sym) => {
            const st = (g.assets && g.assets[sym]) || {};
            if (!st.n) {
              return `<div class="macro-stat-line"><strong>${escHtml(st.label || sym)}</strong>：樣本不足（n=0）</div>`;
            }
            const sign = st.avg_pct > 0 ? "+" : "";
            const cls = st.avg_pct > 0 ? "chg-up" : st.avg_pct < 0 ? "chg-down" : "";
            return `<div class="macro-stat-line"><strong>${escHtml(st.label || sym)}</strong>：平均 <span class="${cls}">${sign}${Number(st.avg_pct).toFixed(2)}%</span>、上漲機率 ${Math.round(Number(st.up_prob_pct))}%（n=${st.n}）</div>`;
          })
          .join("");
        return `<div class="macro-stat-box"><div class="macro-stat-box-title">${escHtml(g.group)}時（${g.n_rows} 次）</div>${lines}</div>`;
      })
      .join("");
    return `${introNote ? `<p class="muted" style="margin:12px 0 6px;">${escHtml(introNote)}</p>` : ""}<div class="macro-hist-stats">${cards}</div>`;
  }

  function buildHistModalBodyHtml(card, methodology) {
    const rows = card.history_rows || [];
    const tbody = rows
      .map((r) => {
        const consCell = r.consensus_str
          ? escHtml(r.consensus_str)
          : '<span class="muted">無可靠預期值</span>';
        const surpCls =
          r.surprise === "高於預期" || r.surprise === "高於前值"
            ? "surp-hi"
            : r.surprise === "低於預期" || r.surprise === "低於前值"
              ? "surp-lo"
              : "surp-eq";
        const noteSub = r.note ? `<div class="muted" style="font-size:0.72rem;white-space:normal;">${escHtml(r.note)}</div>` : "";
        const rx = r.reactions || {};
        return `
          <tr>
            <td>${escHtml(r.release_taipei)}</td>
            <td>${escHtml(r.period_zh || r.period)}${noteSub}</td>
            <td>${escHtml(r.actual_str)}</td>
            <td>${escHtml(r.prev_str)}</td>
            <td>${consCell}</td>
            <td class="${surpCls}">${escHtml(r.surprise)}</td>
            ${formatPctCell(rx.NAS100)}
            ${formatPctCell(rx.SP500)}
            ${formatPctCell(rx.TWII)}
            ${formatPctCell(rx.BTCUSDT)}
          </tr>
        `;
      })
      .join("");

    const stats = card.history_stats || {};
    let statsHtml = "";
    if (stats.primary_grouping === "consensus" && Array.isArray(stats.by_consensus) && stats.by_consensus.length) {
      statsHtml += renderStatsGroupHtml(
        stats.by_consensus,
        `依「高於／符合／低於預期」分組統計（有可靠預期值樣本 ${stats.consensus_rows_count} 次；其餘較早期間標示無可靠預期值）：`
      );
      if (Array.isArray(stats.by_prev) && stats.by_prev.length) {
        statsHtml += `
          <details class="macro-fold" style="margin-top:12px;">
            <summary style="font-size:0.86rem;color:var(--accent);">查看全部 ${stats.total_rows_count} 次依「高於／低於前值」統計</summary>
            ${renderStatsGroupHtml(stats.by_prev, "")}
          </details>
        `;
      }
    } else {
      statsHtml += renderStatsGroupHtml(
        stats.by_prev || [],
        "本指標歷史多數期別無可靠市場共識預期值，改依「高於／低於前值」分組統計："
      );
    }

    const meth = methodology || {};
    return `
      <div class="macro-hist-wrap">
        <table class="macro-hist-table">
          <thead>
            <tr>
              <th>公布時間（台北）</th>
              <th>所屬期間</th>
              <th>實際值</th>
              <th>前值</th>
              <th>預期值</th>
              <th>比較結果</th>
              <th>那指 100</th>
              <th>標普 500</th>
              <th>台股加權</th>
              <th>BTC</th>
            </tr>
          </thead>
          <tbody>${tbody || '<tr><td colspan="10" class="center muted">尚無歷史紀錄</td></tr>'}</tbody>
        </table>
      </div>
      ${statsHtml}
      <div class="macro-hist-foot">
        <strong>口徑說明（${escHtml(meth.source || "本站 history_1d.json.gz")}）：</strong><br/>
        • 那指 100（NAS100）／標普 500（SP500）：${escHtml(meth.NAS100_SP500 || "公布當日美股收盤對前一交易日收盤")}<br/>
        • 台股加權（TWII）：${escHtml(meth.TWII || "公布後第一個台股交易日收盤對前一日收盤")}<br/>
        • BTC（BTCUSDT）：${escHtml(meth.BTCUSDT || "公布前最後一根 UTC 日 K 收盤對公布後第一根 UTC 日 K 收盤")}<br/>
        ${card.modal_foot_extra ? `• ${escHtml(card.modal_foot_extra)}<br/>` : ""}
        • 歷史市場反應僅供統計參考，過去表現不代表未來報酬。
      </div>
    `;
  }

  function openHistModal(indicatorId) {
    if (!cachedMacro) return;
    const card = (cachedMacro.indicators || []).find((c) => c.id === indicatorId);
    if (!card) return;
    const backdrop = document.getElementById("macro-hist-modal-backdrop");
    const titleEl = document.getElementById("macro-hist-modal-title");
    const bodyEl = document.getElementById("macro-hist-modal-body");
    lastFocusEl = document.activeElement;
    const nRows = (card.history_rows || []).length;
    titleEl.textContent = `${card.modal_title || card.name}｜過去 ${nRows} 次公布後市場怎麼走`;
    bodyEl.innerHTML = buildHistModalBodyHtml(card, cachedMacro.reaction_methodology);
    backdrop.classList.remove("hidden");
    if (document.body && document.body.style) document.body.style.overflow = "hidden";
    const closeBtn = document.getElementById("macro-hist-modal-close");
    if (closeBtn && closeBtn.focus) closeBtn.focus();
  }

  function closeHistModal() {
    const backdrop = document.getElementById("macro-hist-modal-backdrop");
    const bodyEl = document.getElementById("macro-hist-modal-body");
    backdrop.classList.add("hidden");
    bodyEl.innerHTML = "";
    if (document.body && document.body.style) document.body.style.overflow = "";
    if (lastFocusEl && lastFocusEl.focus) lastFocusEl.focus();
  }

  function downloadIcs(title, isoTaipei, desc) {
    const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(isoTaipei || "");
    if (!m) return;
    const p = { y: +m[1], mo: +m[2], d: +m[3], h: +m[4], mi: +m[5] };
    const dur = title.includes("FOMC 利率決議") ? 60 : 30;
    const endDate = new Date(Date.UTC(p.y, p.mo - 1, p.d, p.h, p.mi) + dur * 60000);
    const p2 = (n) => String(n).padStart(2, "0");
    const startLoc = `${p.y}${p2(p.mo)}${p2(p.d)}T${p2(p.h)}${p2(p.mi)}00`;
    const endLoc = `${endDate.getUTCFullYear()}${p2(endDate.getUTCMonth() + 1)}${p2(endDate.getUTCDate())}T${p2(endDate.getUTCHours())}${p2(endDate.getUTCMinutes())}00`;
    const esc = (s) => String(s || "").replace(/\\/g, "\\\\").replace(/;/g, "\\;").replace(/,/g, "\\,").replace(/\r?\n/g, "\\n");
    const now = new Date();
    const dtstamp = `${now.getUTCFullYear()}${p2(now.getUTCMonth() + 1)}${p2(now.getUTCDate())}T${p2(now.getUTCHours())}${p2(now.getUTCMinutes())}${p2(now.getUTCSeconds())}Z`;
    const lines = [
      "BEGIN:VCALENDAR",
      "VERSION:2.0",
      "PRODID:-//SignalDesk//Macro Calendar//ZH-TW",
      "CALSCALE:GREGORIAN",
      "METHOD:PUBLISH",
      "BEGIN:VTIMEZONE",
      "TZID:Asia/Taipei",
      "BEGIN:STANDARD",
      "TZOFFSETFROM:+0800",
      "TZOFFSETTO:+0800",
      "TZNAME:CST",
      "DTSTART:19700101T000000",
      "END:STANDARD",
      "END:VTIMEZONE",
      "BEGIN:VEVENT",
      `UID:signaldesk-${startLoc}-${Math.abs(title.length * 997)}@pandatesttrading`,
      `DTSTAMP:${dtstamp}`,
      `DTSTART;TZID=Asia/Taipei:${startLoc}`,
      `DTEND;TZID=Asia/Taipei:${endLoc}`,
      `SUMMARY:${esc(title)}`,
      desc ? `DESCRIPTION:${esc(desc)}` : "",
      "BEGIN:VALARM",
      "ACTION:DISPLAY",
      `DESCRIPTION:${esc(title)}`,
      "TRIGGER:-PT30M",
      "END:VALARM",
      "END:VEVENT",
      "END:VCALENDAR",
    ].filter(Boolean);
    const blob = new Blob([lines.join("\r\n") + "\r\n"], { type: "text/calendar;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${title.replace(/[\\/:*?"<>|]/g, "")}-${startLoc.slice(0, 8)}.ics`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(() => URL.revokeObjectURL(url), 4000);
  }

  const FEDWATCH_BUCKET_DEFS = [
    { key: "cut_2_plus", label: "降 2 碼以上" },
    { key: "cut_1", label: "降 1 碼" },
    { key: "hold", label: "維持" },
    { key: "hike_1", label: "升 1 碼" },
    { key: "hike_2_plus", label: "升 2 碼以上" },
  ];

  function fmtProbPct(val) {
    const n = Number(val || 0);
    return Math.abs(n - Math.round(n)) < 0.05 ? `${Math.round(n)}%` : `${n.toFixed(1)}%`;
  }

  function renderAssetImpacts(impacts) {
    const list = Array.isArray(impacts) ? impacts : [];
    const pillsEl = document.getElementById("macro-asset-impacts");
    const detailEl = document.getElementById("macro-asset-detail");
    if (!pillsEl) return;

    if (!list.length) {
      pillsEl.innerHTML = '<span class="muted" style="font-size:0.82rem;">對各資產影響：資料更新中</span>';
      if (detailEl) detailEl.classList.add("hidden");
      return;
    }

    if (activeAssetImpactIdx != null && (activeAssetImpactIdx < 0 || activeAssetImpactIdx >= list.length)) {
      activeAssetImpactIdx = null;
    }

    pillsEl.innerHTML = list
      .map((imp, idx) => {
        const isSel = activeAssetImpactIdx === idx;
        const cls = imp.cls || "neut";
        return `
          <button type="button" class="macro-asset-pill${isSel ? " active" : ""}" data-asset-idx="${idx}" aria-expanded="${isSel ? "true" : "false"}">
            <span class="macro-asset-pill-name">${escHtml(imp.asset)}</span>
            <span class="macro-pill-tag ${escAttr(cls)}">${escHtml(imp.tag || "中性")}</span>
          </button>
        `;
      })
      .join("");

    if (detailEl) {
      if (activeAssetImpactIdx != null && list[activeAssetImpactIdx]) {
        const imp = list[activeAssetImpactIdx];
        detailEl.innerHTML = `
          <div class="macro-asset-detail-head">
            <strong>${escHtml(imp.asset)}</strong>
            ${imp.sub ? `<span class="muted">${escHtml(imp.sub)}</span>` : ""}
            <span class="macro-pill-tag ${escAttr(imp.cls || "neut")}">${escHtml(imp.tag || "")}</span>
          </div>
          <p class="macro-asset-detail-note">${escHtml(imp.note || "資料不足，暫不判斷")}</p>
        `;
        detailEl.classList.remove("hidden");
      } else {
        detailEl.innerHTML = "";
        detailEl.classList.add("hidden");
      }
    }
  }

  function buildSnapshotFallbackItems() {
    return [
      { id: "DXY", name_zh: "美元指數", last_str: "資料更新中", chg_1d_pct: null, verdict: "na", verdict_tag: "資料更新中", updating: true },
      { id: "USDTWD", name_zh: "美元／台幣", last_str: "資料更新中", chg_1d_pct: null, verdict: "na", verdict_tag: "資料更新中", updating: true },
      { id: "GOLD", name_zh: "黃金", last_str: "資料更新中", chg_1d_pct: null, verdict: "na", verdict_tag: "資料更新中", updating: true },
      { id: "WTI", name_zh: "原油 (WTI)", last_str: "資料更新中", chg_1d_pct: null, verdict: "na", verdict_tag: "資料更新中", updating: true },
      { id: "US10Y", name_zh: "美國 10 年期殖利率", last_str: "資料更新中", chg_1d_pct: null, verdict: "na", verdict_tag: "資料更新中", updating: true },
    ];
  }

  function renderMarketSnapshot() {
    const snap = cachedMacro && cachedMacro.market_snapshot;
    const gridEl = document.getElementById("macro-snapshot-grid");
    const timeEl = document.getElementById("macro-snapshot-time");
    if (!gridEl) return;

    const hhmm = (snap && snap.hhmm_taipei) || (cachedMacro && cachedMacro.updated_at_taipei ? String(cachedMacro.updated_at_taipei).slice(11, 16) : "--:--");
    if (timeEl) timeEl.textContent = `資料時間：台北 ${hhmm}`;

    const items = snap && Array.isArray(snap.items) && snap.items.length ? snap.items : buildSnapshotFallbackItems();
    gridEl.innerHTML = items
      .map((it) => {
        const hasVal = it.last != null && it.last_str !== "資料更新中";
        let chgHtml = '<span class="muted">資料更新中</span>';
        if (hasVal) {
          if (it.id === "US10Y" && it.chg_1d_pp != null) {
            const pp = Number(it.chg_1d_pp);
            const cls = pp > 0 ? "chg-up" : pp < 0 ? "chg-down" : "muted";
            const sign = pp > 0 ? "+" : "";
            chgHtml = `<span class="macro-snap-chg ${cls}">${sign}${pp.toFixed(2)}pp</span>`;
          } else if (it.chg_1d_pct != null) {
            const pct = Number(it.chg_1d_pct);
            const cls = pct > 0 ? "chg-up" : pct < 0 ? "chg-down" : "muted";
            const sign = pct > 0 ? "+" : "";
            chgHtml = `<span class="macro-snap-chg ${cls}">${sign}${pct.toFixed(2)}%</span>`;
          }
        }
        const tagText = it.updating && !hasVal ? "資料更新中" : (it.verdict_tag || "中性");
        const tagCls = it.verdict || "neut";
        return `
          <button type="button" class="macro-snap-card" data-snap-id="${escAttr(it.id)}">
            <div class="macro-snap-name">${escHtml(it.name_zh || it.id)}</div>
            <div class="macro-snap-val-row macro-num">
              <span class="macro-snap-val">${escHtml(it.last_str || "資料更新中")}</span>
              ${hasVal ? chgHtml : ""}
            </div>
            <div class="macro-snap-verdict"><span class="macro-pill-tag ${escAttr(tagCls)}">${escHtml(tagText)}</span></div>
          </button>
        `;
      })
      .join("");
  }

  function openSnapshotModal(snapId) {
    const snap = cachedMacro && cachedMacro.market_snapshot;
    const items = (snap && Array.isArray(snap.items) && snap.items) || buildSnapshotFallbackItems();
    const it = items.find((x) => x.id === snapId);
    if (!it) return;
    const backdrop = document.getElementById("macro-hist-modal-backdrop");
    const titleEl = document.getElementById("macro-hist-modal-title");
    const bodyEl = document.getElementById("macro-hist-modal-body");
    lastFocusEl = document.activeElement;
    titleEl.textContent = `${it.name_zh || it.id}（${it.symbol || ""}）｜市場快照詳情`;

    const hasVal = it.last != null && it.last_str !== "資料更新中";
    const chg20Str =
      it.chg_20d_pct != null
        ? (it.id === "US10Y" && it.chg_20d_pp != null
            ? `${it.chg_20d_pp > 0 ? "+" : ""}${Number(it.chg_20d_pp).toFixed(2)} 個百分點（${it.chg_20d_pct > 0 ? "+" : ""}${Number(it.chg_20d_pct).toFixed(2)}%）`
            : `${it.chg_20d_pct > 0 ? "+" : ""}${Number(it.chg_20d_pct).toFixed(2)}%`)
        : "資料更新中";
    const posStr =
      it.range_1y_pos_pct != null
        ? `位於一年區間 ${Math.round(Number(it.range_1y_pos_pct))}% 位置（低 ${escHtml(it.range_1y_low)} ～ 高 ${escHtml(it.range_1y_high)}）`
        : "資料更新中";
    const posPct = it.range_1y_pos_pct != null ? Math.max(0, Math.min(100, Number(it.range_1y_pos_pct))) : 0;

    bodyEl.innerHTML = `
      <div class="macro-snap-modal-top">
        <div>
          <div class="muted" style="font-size:0.8rem;">最新報價（${escHtml(it.as_of_date || "--")}）</div>
          <div class="macro-ind-val macro-num">${escHtml(it.last_str || "資料更新中")}${it.unit && it.unit !== "%" ? ` <span class="macro-ind-unit">${escHtml(it.unit)}</span>` : ""}</div>
        </div>
        <span class="macro-pill-tag ${escAttr(it.verdict || "neut")}">${escHtml(it.verdict_tag || "中性")}</span>
      </div>
      ${it.updating ? `<div class="macro-updating-pill" style="margin-bottom:10px;">⚠ 資料更新中（暫保留前次資料）</div>` : ""}
      <div class="macro-snap-modal-kv">
        <div><span class="k">近 20 日變動</span><span class="v macro-num">${escHtml(chg20Str)}</span></div>
        <div><span class="k">近一年區間位置</span><span class="v macro-num">${posStr}</span></div>
      </div>
      ${hasVal && it.range_1y_pos_pct != null ? `
        <div class="macro-range-bar-wrap" aria-hidden="true">
          <div class="macro-range-bar-track"><div class="macro-range-bar-fill" style="width:${posPct}%"></div><div class="macro-range-bar-dot" style="left:${posPct}%"></div></div>
        </div>
      ` : ""}
      <div class="macro-spark-box" style="margin:12px 0;">
        ${buildSparklineSvg(it.closes_90d)}
        <div class="macro-spark-cap"><span>近 90 個交易日走勢</span><span>最新 ${escHtml(it.last_str || "--")}</span></div>
      </div>
      <div class="macro-snap-modal-desc">
        <div><strong>是什麼：</strong>${escHtml(it.what_is || "—")}</div>
        <div><strong>判定理由（對台股／加密貨幣視角）：</strong>${escHtml(it.verdict_reason || "—")}</div>
        ${it.rule_rationale ? `<div class="muted" style="font-size:0.8rem;"><strong>門檻說明：</strong>${escHtml(it.rule_rationale)}</div>` : ""}
        <div class="muted" style="font-size:0.78rem;">來源：${escHtml(it.source_used || "資料更新中")}｜更新時間（台北）：${escHtml(it.as_of_taipei || "--")}</div>
      </div>
    `;
    backdrop.classList.remove("hidden");
    if (document.body && document.body.style) document.body.style.overflow = "hidden";
    const closeBtn = document.getElementById("macro-hist-modal-close");
    if (closeBtn && closeBtn.focus) closeBtn.focus();
  }

  function renderFedWatch() {
    const fw = cachedMacro && cachedMacro.fedwatch;
    const upcoming = (cachedMacro.calendar && cachedMacro.calendar.upcoming) || [];
    const nowMs = Date.now();
    const nextFomc = upcoming.find(
      (e) => e.indicator_id === "FED_RATE" && !String(e.id || "").includes("blackout") && !String(e.id || "").includes("press") && !String(e.id || "").includes("fomc-min") && !String(e.id || "").includes("beige") && new Date(e.time_utc).getTime() > nowMs
    );
    const fomcLineEl = document.getElementById("macro-fomc-next-line");
    if (fomcLineEl) {
      if (nextFomc) {
        fomcLineEl.innerHTML = `下次 FOMC：<strong>${escHtml(nextFomc.short_taipei)}</strong> · <span class="macro-cd" data-cd="${escAttr(nextFomc.time_taipei_iso)}">${escHtml(formatCountdown(nextFomc.time_taipei_iso, nowMs).text)}</span>`;
      } else {
        fomcLineEl.textContent = "下次 FOMC：載入排程中";
      }
    }

    const sumEl = document.getElementById("macro-fedwatch-summary");
    const panelEl = document.getElementById("macro-fedwatch-panel");
    const toggleBtn = document.getElementById("macro-fedwatch-toggle");
    const isOpen = getBoolPref(LS_FEDWATCH_OPEN, false);
    if (toggleBtn) {
      toggleBtn.setAttribute("aria-expanded", isOpen ? "true" : "false");
      toggleBtn.textContent = isOpen ? "收起 ▴" : "展開 ▾";
    }
    if (panelEl) panelEl.classList.toggle("hidden", !isOpen);

    const meetings = (fw && Array.isArray(fw.meetings) && fw.meetings) || [];
    if (!meetings.length) {
      if (sumEl) sumEl.textContent = "FedWatch：資料更新中";
      if (panelEl) {
        panelEl.innerHTML = '<div class="muted" style="font-size:0.84rem;padding:8px 0;">FedWatch 期貨利率資料更新中（請稍候或執行每日更新）。</div>';
      }
      return;
    }

    const m0 = meetings[0];
    const statusBadge = fw.updating
      ? ' <span class="macro-updating-pill" style="margin:0 0 0 6px;">資料更新中</span>'
      : (String(fw.source_used || "").includes("手動更新")
          ? ' <span class="macro-manual-pill">手動更新</span>'
          : "");
    if (sumEl) {
      sumEl.innerHTML = `${escHtml(m0.summary_text || "FedWatch 市場押注")}${statusBadge}`;
    }

    if (panelEl) {
      const meetingCards = meetings
        .map((m) => {
          const probs = m.probabilities || {};
          let otherProb = 0;
          let maxKey = null;
          let maxVal = -1;
          const rows = [];
          FEDWATCH_BUCKET_DEFS.forEach((b) => {
            const p = Number(probs[b.key] || 0);
            if (p > maxVal) {
              maxVal = p;
              maxKey = b.key;
            }
            if (p > 0.5) {
              rows.push({ key: b.key, label: b.label, prob: p });
            } else if (p > 0) {
              otherProb += p;
            }
          });
          if (otherProb > 0.05) {
            rows.push({ key: "other", label: "其他", prob: Math.round(otherProb * 10) / 10 });
          }
          const barsHtml = rows
            .map((r) => {
              const isTop = r.key === maxKey;
              const w = Math.max(2, Math.min(100, r.prob));
              return `
                <div class="macro-fw-row${isTop ? " top" : ""}">
                  <span class="macro-fw-lbl">${escHtml(r.label)}</span>
                  <div class="macro-fw-track"><div class="macro-fw-fill${isTop ? " top" : ""}" style="width:${w}%"></div></div>
                  <span class="macro-fw-pct macro-num">${escHtml(fmtProbPct(r.prob))}</span>
                </div>
              `;
            })
            .join("");
          const impRateStr = m.implied_rate != null ? `${Number(m.implied_rate).toFixed(3)}%` : "--";
          return `
            <div class="macro-fw-card">
              <div class="macro-fw-card-head">
                <strong>${escHtml(m.date_taipei || m.date)}｜${escHtml(m.title || "FOMC 會議")}</strong>
                <span class="muted macro-num" style="font-size:0.78rem;">目前區間 ${escHtml(m.current_range || fw.current_range || "--")} · 隱含利率 ${escHtml(impRateStr)}</span>
              </div>
              <div class="macro-fw-bars">${barsHtml}</div>
            </div>
          `;
        })
        .join("");

      panelEl.innerHTML = `
        <div class="macro-fw-grid">${meetingCards}</div>
        <div class="macro-fw-foot">
          <span>${escHtml(fw.disclaimer || "機率由聯邦基金期貨價格推算，方法同 CME FedWatch；僅代表市場押注，不是預測。")}</span>
          <span class="muted">資料時間（台北）：${escHtml(fw.as_of_taipei || "--")}｜來源：${escHtml(fw.source_used || "資料更新中")}</span>
        </div>
      `;
    }
  }

  function applyOverviewStanceFold() {
    const isOpen = getBoolPref(LS_STANCE_OPEN, false);
    const btn = document.getElementById("macro-stance-toggle");
    const box = document.getElementById("macro-stance-detail-box");
    const brief = document.getElementById("macro-stance-brief");
    if (btn) {
      btn.setAttribute("aria-expanded", isOpen ? "true" : "false");
      btn.textContent = isOpen ? "收起 ▴" : "說明 ▾";
    }
    if (box) box.classList.toggle("hidden", !isOpen);
    if (brief) brief.classList.toggle("hidden", isOpen);
  }

  function renderOverview() {
    const ov = cachedMacro.overview || {};
    const badge = document.getElementById("macro-stance-badge");
    badge.className = `macro-pill-tag macro-stance-xl ${ov.stance_cls || "neut"}`;
    badge.textContent = ov.stance || "中性觀察";
    const fullDesc = ov.stance_desc || "";
    const descEl = document.getElementById("macro-stance-desc");
    if (descEl) descEl.textContent = fullDesc;
    const briefEl = document.getElementById("macro-stance-brief");
    if (briefEl) {
      const firstSent = fullDesc.split("。")[0];
      briefEl.textContent = firstSent ? `${firstSent}。` : "點擊下方標籤查看各資產影響判讀。";
    }
    applyOverviewStanceFold();

    renderAssetImpacts(ov.asset_impacts || []);
    renderMarketSnapshot();
    renderFedWatch();

    const upcoming = (cachedMacro.calendar && cachedMacro.calendar.upcoming) || [];
    const nowMs = Date.now();
    const majorUpcoming = upcoming
      .filter((e) => new Date(e.time_utc).getTime() > nowMs && Number(e.stars || 0) >= 3)
      .slice(0, 3);
    const nextEl = document.getElementById("macro-overview-next-grid");
    if (!majorUpcoming.length) {
      nextEl.innerHTML = '<div class="empty-state">暫無即將公布的重要事件</div>';
    } else {
      nextEl.innerHTML = majorUpcoming
        .map(
          (ev) => `
          <div class="macro-next-card">
            <div class="macro-next-when">${escHtml(ev.short_taipei)} <span class="macro-cal-stars">${"★".repeat(ev.stars || 1)}</span></div>
            <div class="macro-next-title">${escHtml(ev.title)}</div>
            <div class="macro-cd" data-cd="${escAttr(ev.time_taipei_iso)}">計算中…</div>
            <div class="muted" style="font-size:0.78rem;">${escHtml(ev.note || (ev.consensus_str ? `市場預期 ${ev.consensus_str}` : "點擊日曆分頁查看更多時程"))}</div>
            <div><button type="button" class="macro-ics-btn" data-ics-title="${escAttr(ev.title)}" data-ics-start="${escAttr(ev.time_taipei_iso)}" data-ics-desc="${escAttr(ev.note || "")}">+ 加入行事曆</button></div>
          </div>
        `
        )
        .join("");
    }

    const byId = {};
    (cachedMacro.indicators || []).forEach((c) => {
      byId[c.id] = c;
    });
    const statCards = [
      { key: "FED_RATE", label: "聯邦基金利率" },
      { key: "CPI", label: "CPI 消費者物價" },
      { key: "UNRATE", label: "美國失業率" },
      { key: "DGS10", label: "10 年期美債殖利率" },
    ];
    document.getElementById("macro-overview-stats").innerHTML = statCards
      .map((s) => {
        const c = byId[s.key];
        if (!c) return "";
        return `
          <div class="summary-card">
            <span class="metric-label">${escHtml(s.label)}（${escHtml(c.period)}）</span>
            <span class="summary-val macro-num">${escHtml(c.value_str)}</span>
            <span class="muted" style="font-size:0.78rem;">前值 ${escHtml(c.prev_str)}｜變化 ${escHtml(c.change_str)}</span>
          </div>
        `;
      })
      .join("");
  }

  function renderIndicators() {
    const groups = { rates: [], inflation: [], jobs: [], growth: [] };
    (cachedMacro.indicators || []).forEach((c) => {
      if (groups[c.category]) groups[c.category].push(c);
    });
    Object.keys(groups).forEach((cat) => {
      const el = document.getElementById(`macro-grid-${cat}`);
      if (!el) return;
      el.innerHTML = groups[cat].map(renderIndicatorCardHtml).join("") || '<div class="empty-state">暫無指標資料</div>';
    });
  }

  function populateCalendarControls() {
    const indSel = document.getElementById("macro-cal-indicator");
    const monthSel = document.getElementById("macro-cal-past-month");
    if (indSel && indSel.options.length <= 1) {
      const opts = [
        ["all", "全部指標"],
        ["FED_RATE", "FOMC / 聯準會"],
        ["CPI", "CPI / 核心 CPI"],
        ["PCE", "PCE / 核心 PCE"],
        ["PPI", "PPI 生產者物價"],
        ["NFP", "非農 / 失業率 / 時薪"],
        ["ICSA", "初領失業金人數"],
        ["JOLTS", "JOLTS 職位空缺"],
        ["GDP", "GDP 成長率"],
        ["ISM_MFG", "ISM 製造業 PMI"],
        ["ISM_SVC", "ISM 服務業 PMI"],
        ["RETAIL_SALES", "零售銷售"],
      ];
      indSel.innerHTML = opts.map(([v, l]) => `<option value="${v}">${l}</option>`).join("");
      indSel.value = calIndicator;
    }

    if (monthSel) {
      const past = (cachedMacro.calendar && cachedMacro.calendar.past) || [];
      const months = getAvailablePastMonths(past);
      const curVal = calPastMonth;
      monthSel.innerHTML =
        '<option value="recent7">最近 7 天（預設）</option>' +
        months
          .map((ym) => {
            const parts = ym.split("-");
            return `<option value="${ym}">${parts[0]} 年 ${Number(parts[1])} 月</option>`;
          })
          .join("");
      monthSel.value = curVal === "recent7" || months.includes(curVal) ? curVal : "recent7";
      calPastMonth = monthSel.value;
    }
  }

  function renderCalendar() {
    populateCalendarControls();
    const cal = cachedMacro.calendar || {};
    const upcoming = cal.upcoming || [];
    const past = cal.past || [];
    const nowMs = Date.now();

    // 1. Top Hero Card: next important ★★★ event (or nearest upcoming)
    const nextHero =
      upcoming.find((e) => new Date(e.time_utc).getTime() > nowMs && Number(e.stars || 0) >= 3) ||
      upcoming.find((e) => new Date(e.time_utc).getTime() > nowMs);
    const heroEl = document.getElementById("macro-cal-hero");
    if (nextHero) {
      heroEl.innerHTML = `
        <div>
          <div class="macro-cal-hero-k">下一個重要公布事件（台北時間） · <span class="macro-cal-stars">${"★".repeat(nextHero.stars || 1)}</span></div>
          <div class="macro-cal-hero-title">${escHtml(nextHero.title)}</div>
          <div class="muted">公布時間：<strong>${escHtml(nextHero.short_taipei)}</strong>${nextHero.consensus_str ? `｜市場預期 ${escHtml(nextHero.consensus_str)}` : ""}${nextHero.note ? `｜${escHtml(nextHero.note)}` : ""}</div>
        </div>
        <div style="text-align:right;display:flex;flex-direction:column;align-items:flex-end;gap:8px;">
          <div class="macro-cal-hero-cd" data-cd="${escAttr(nextHero.time_taipei_iso)}">計算中…</div>
          <button type="button" class="macro-ics-btn" data-ics-title="${escAttr(nextHero.title)}" data-ics-start="${escAttr(nextHero.time_taipei_iso)}" data-ics-desc="${escAttr(nextHero.note || "")}">+ 加入行事曆 (.ics)</button>
        </div>
      `;
    } else {
      heroEl.innerHTML = '<div><div class="macro-cal-hero-title">暫無即將公布事件</div></div>';
    }

    // 2. Upcoming events (filtered by segmented range + stars + indicator)
    const rangeLabels = {
      week2: "本週與下週",
      this_week: "本週",
      next_week: "下週",
      "60d": "未來 60 天",
    };
    const filteredUp = filterUpcomingEvents(upcoming, { nowMs });
    document.getElementById("macro-cal-up-label").textContent = rangeLabels[calRange] || "本週與下週";
    document.getElementById("macro-cal-up-count").textContent = `${filteredUp.length} 筆`;
    const upEl = document.getElementById("macro-cal-upcoming-list");
    if (!filteredUp.length) {
      upEl.innerHTML = `<div class="empty-state">${escHtml(rangeLabels[calRange] || "此區間")}內無符合篩選條件的事件（可切換「未來 60 天」或「全部重要程度」）</div>`;
    } else {
      upEl.innerHTML = filteredUp
        .map(
          (ev) => `
          <div class="macro-cal-row">
            <span class="macro-cal-when macro-num">${escHtml(ev.short_taipei)}</span>
            <div>
              <span class="macro-cal-stars">${"★".repeat(ev.stars || 1)}</span>
              <strong>${escHtml(ev.title)}</strong>
              ${ev.consensus_str ? `<span class="muted"> · 預期 ${escHtml(ev.consensus_str)}</span>` : ""}
              ${ev.note ? `<span class="muted"> · ${escHtml(ev.note)}</span>` : ""}
              <button type="button" class="macro-ics-btn" style="margin-left:6px;" data-ics-title="${escAttr(ev.title)}" data-ics-start="${escAttr(ev.time_taipei_iso)}" data-ics-desc="${escAttr(ev.note || "")}">加入行事曆</button>
            </div>
            <span class="macro-cd" data-cd="${escAttr(ev.time_taipei_iso)}">${escHtml(formatCountdown(ev.time_taipei_iso, nowMs).text)}</span>
          </div>
        `
        )
        .join("");
    }

    // 3. Past events (default recent 7 days, compact 1-line summary + click to expand)
    const filteredPast = filterPastEvents(past, { nowMs });
    const pastLabel = calPastMonth === "recent7" ? "最近 7 天" : `${calPastMonth.replace("-", " 年 ")} 月`;
    document.getElementById("macro-cal-past-label").textContent = pastLabel;
    const pastEl = document.getElementById("macro-cal-past-list");
    if (!filteredPast.length) {
      pastEl.innerHTML = `<div class="empty-state">${escHtml(pastLabel)}無符合篩選條件的已公布紀錄（可切換月份下拉選單或「全部重要程度」）</div>`;
    } else {
      pastEl.innerHTML = filteredPast
        .map((ev) => {
          const isOpen = expandedPastIds.has(ev.id);
          const rx = ev.reactions || {};
          const cmpTarget = ev.consensus_str ? `預期 ${escHtml(ev.consensus_str)}` : `前值 ${escHtml(ev.prev_str)}`;
          const surpCls =
            ev.surprise === "高於預期" || ev.surprise === "高於前值" || ev.surprise === "偏鷹"
              ? "bear"
              : ev.surprise === "低於預期" || ev.surprise === "低於前值"
                ? "bull"
                : "neut";
          return `
            <div class="macro-cal-past-item" data-past-id="${escAttr(ev.id)}">
              <button type="button" class="macro-cal-past-summary" aria-expanded="${isOpen ? "true" : "false"}">
                <span class="mcp-when macro-num">${escHtml(ev.short_taipei)}</span>
                <span class="mcp-title"><span class="macro-cal-stars">${"★".repeat(ev.stars || 1)}</span>${escHtml(ev.title)}</span>
                <span class="mcp-vals macro-num">實際 <strong>${escHtml(ev.actual_str)}</strong> <span class="muted">vs ${cmpTarget}</span></span>
                <span class="macro-pill-tag ${surpCls}">${escHtml(ev.surprise)}</span>
                <span class="macro-react-chips">
                  ${formatPctChip("那指", rx.NAS100)}
                  ${formatPctChip("台股", rx.TWII)}
                  ${formatPctChip("BTC", rx.BTCUSDT)}
                </span>
                <span class="muted" aria-hidden="true">${isOpen ? "▴" : "▾"}</span>
              </button>
              <div class="macro-cal-past-detail ${isOpen ? "" : "hidden"}">
                <div><strong>完整公布時間（台北）：</strong>${escHtml(ev.time_taipei)}｜<strong>重要程度：</strong>${"★".repeat(ev.stars || 1)}</div>
                <div><strong>實際：</strong>${escHtml(ev.actual_str)}｜<strong>前值：</strong>${escHtml(ev.prev_str)}｜<strong>市場預期：</strong>${ev.consensus_str ? `${escHtml(ev.consensus_str)}（${escHtml(ev.consensus_source || "公開共識")}）` : "無可靠預期值（改用前值比較）"}</div>
                <div><strong>全市場反應：</strong>那指 100 ${rx.NAS100 != null ? `${rx.NAS100 > 0 ? "+" : ""}${rx.NAS100.toFixed(2)}%` : "—"}｜標普 500 ${rx.SP500 != null ? `${rx.SP500 > 0 ? "+" : ""}${rx.SP500.toFixed(2)}%` : "—"}｜台股加權 ${rx.TWII != null ? `${rx.TWII > 0 ? "+" : ""}${rx.TWII.toFixed(2)}%` : "—"}｜BTC ${rx.BTCUSDT != null ? `${rx.BTCUSDT > 0 ? "+" : ""}${rx.BTCUSDT.toFixed(2)}%` : "—"}</div>
                ${ev.note ? `<div><strong>備註：</strong>${escHtml(ev.note)}</div>` : ""}
              </div>
            </div>
          `;
        })
        .join("");
    }
    tickCountdowns();
  }

  function scanMacroGlossary() {
    const g = window.SIGNAL_DESK_GLOSSARY;
    if (!g || typeof g.scanContainers !== "function" || !g.isEnabled()) return;
    g.scanContainers(document.getElementById("macro-view-overview"), ".macro-overview-hero-card, #macro-fed-section, #macro-logic-fold");
    g.scanContainers(document.getElementById("macro-view-data"), ".macro-ind-card");
    g.scanContainers(document.getElementById("macro-view-calendar"), ".macro-cal-row, .macro-cal-past-item");
  }

  function openGlossaryModal() {
    const g = window.SIGNAL_DESK_GLOSSARY;
    if (!g) return;
    const backdrop = document.getElementById("macro-hist-modal-backdrop");
    const titleEl = document.getElementById("macro-hist-modal-title");
    const bodyEl = document.getElementById("macro-hist-modal-body");
    if (!backdrop || !titleEl || !bodyEl) return;
    lastFocusEl = document.activeElement;
    titleEl.textContent = `交易與總經名詞小辭典（共 ${g.TERMS.length} 則）`;
    g.renderGlossaryModalBody(bodyEl);
    backdrop.classList.remove("hidden");
    if (document.body && document.body.style) document.body.style.overflow = "hidden";
    const closeBtn = document.getElementById("macro-hist-modal-close");
    if (closeBtn && closeBtn.focus) closeBtn.focus();
  }

  function renderAll() {
    if (!cachedMacro) return;
    const updEl = document.getElementById("macro-updated-at");
    if (updEl) updEl.textContent = cachedMacro.updated_at_taipei || "--";

    const stanceEl = document.getElementById("macro-top-stance");
    if (stanceEl) stanceEl.textContent = (cachedMacro.overview && cachedMacro.overview.stance) || "--";

    const light = document.getElementById("macro-data-light");
    const updList = cachedMacro.updating_indicators || [];
    if (light) {
      if (updList.length > 0) {
        light.className = "light light-warn";
        light.textContent = `宏觀資料：部分更新中 (${updList.length})`;
      } else {
        light.className = "light light-ok";
        light.textContent = `宏觀資料：正常 (${(cachedMacro.indicators || []).length} 項)`;
      }
    }

    const banner = document.getElementById("macro-updating-banner");
    if (banner) {
      if (updList.length > 0) {
        banner.textContent = `⚠️ 部分指標來源暫時無法連線（${updList.join("、")}），已保留前次更新資料並標示「資料更新中」。`;
        banner.classList.remove("hidden");
      } else {
        banner.classList.add("hidden");
      }
    }

    const nextHero = ((cachedMacro.calendar && cachedMacro.calendar.upcoming) || []).find(
      (e) => new Date(e.time_utc).getTime() > Date.now() && Number(e.stars || 0) >= 3
    );
    const topNextEl = document.getElementById("macro-top-next");
    if (topNextEl) {
      topNextEl.innerHTML = nextHero
        ? `${escHtml(nextHero.title)} <span class="macro-cd" data-cd="${escAttr(nextHero.time_taipei_iso)}">計算中…</span>`
        : "--";
    }

    const g = window.SIGNAL_DESK_GLOSSARY;
    if (g && typeof g.unwrapAll === "function") g.unwrapAll(document);

    renderOverview();
    renderIndicators();
    renderCalendar();
    tickCountdowns();
    scanMacroGlossary();
  }

  function switchMacroTab(tabName) {
    if (!["overview", "data", "calendar"].includes(tabName)) tabName = "overview";
    activeTab = tabName;
    document.querySelectorAll("#macro-subtabs .macro-subtab").forEach((btn) => {
      const match = btn.getAttribute("data-macro-tab") === tabName;
      btn.classList.toggle("active", match);
      btn.setAttribute("aria-selected", match ? "true" : "false");
    });
    ["overview", "data", "calendar"].forEach((k) => {
      const sec = document.getElementById(`macro-view-${k}`);
      if (sec) sec.classList.toggle("hidden", k !== tabName);
    });
  }

  async function fetchJsonWithTimeout(url) {
    const controller = typeof AbortController !== "undefined" ? new AbortController() : null;
    const timer = setTimeout(() => {
      if (controller) controller.abort();
    }, REQUEST_TIMEOUT_MS);
    try {
      const resp = await fetch(url, controller ? { cache: "no-store", signal: controller.signal } : { cache: "no-store" });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      return await resp.json();
    } finally {
      clearTimeout(timer);
    }
  }

  async function fetchMacroData() {
    try {
      const remote = await fetchJsonWithTimeout(`${DATA_BASE}/macro.json?t=${Date.now()}`);
      if (remote && Array.isArray(remote.indicators)) return remote;
    } catch (_err) {
      // fallback to repo data/macro.json
    }
    return fetchJsonWithTimeout(`./data/macro.json?t=${Date.now()}`);
  }

  async function refreshMacro() {
    const errBox = document.getElementById("macro-error-box");
    try {
      const doc = await fetchMacroData();
      cachedMacro = doc;
      if (errBox) errBox.classList.add("hidden");
      renderAll();
    } catch (err) {
      if (errBox) {
        errBox.textContent = `宏觀資料載入失敗：${err.message}（若先前已載入資料則暫保留顯示）`;
        errBox.classList.remove("hidden");
      }
      const light = document.getElementById("macro-data-light");
      if (light) {
        light.className = "light light-danger";
        light.textContent = "宏觀資料：讀取失敗";
      }
    }
  }

  function bindEvents() {
    const subtabs = document.getElementById("macro-subtabs");
    if (subtabs) {
      subtabs.addEventListener("click", (e) => {
        const btn = e.target.closest ? e.target.closest(".macro-subtab") : null;
        if (!btn) return;
        const t = btn.getAttribute("data-macro-tab");
        switchMacroTab(t);
        if (window.location && typeof history !== "undefined" && history.replaceState) {
          history.replaceState(null, "", `#${t}`);
        }
      });
    }

    const gotoCal = document.getElementById("macro-goto-calendar");
    if (gotoCal) gotoCal.addEventListener("click", () => switchMacroTab("calendar"));
    const gotoData = document.getElementById("macro-goto-data");
    if (gotoData) gotoData.addEventListener("click", () => switchMacroTab("data"));

    const refreshBtn = document.getElementById("macro-refresh-btn");
    if (refreshBtn) refreshBtn.addEventListener("click", refreshMacro);

    const stanceToggle = document.getElementById("macro-stance-toggle");
    if (stanceToggle) {
      stanceToggle.addEventListener("click", () => {
        setBoolPref(LS_STANCE_OPEN, !getBoolPref(LS_STANCE_OPEN, false));
        applyOverviewStanceFold();
      });
    }

    const fwToggle = document.getElementById("macro-fedwatch-toggle");
    if (fwToggle) {
      fwToggle.addEventListener("click", () => {
        setBoolPref(LS_FEDWATCH_OPEN, !getBoolPref(LS_FEDWATCH_OPEN, false));
        if (cachedMacro) renderFedWatch();
      });
    }

    const logicFold = document.getElementById("macro-logic-fold");
    if (logicFold) {
      logicFold.open = getBoolPref(LS_LOGIC_OPEN, false);
      logicFold.addEventListener("toggle", () => {
        setBoolPref(LS_LOGIC_OPEN, !!logicFold.open);
      });
    }

    const assetImpactsEl = document.getElementById("macro-asset-impacts");
    if (assetImpactsEl) {
      assetImpactsEl.addEventListener("click", (e) => {
        const btn = e.target.closest ? e.target.closest(".macro-asset-pill") : null;
        if (!btn) return;
        const idx = Number(btn.getAttribute("data-asset-idx"));
        if (!Number.isFinite(idx)) return;
        activeAssetImpactIdx = activeAssetImpactIdx === idx ? null : idx;
        renderAssetImpacts((cachedMacro && cachedMacro.overview && cachedMacro.overview.asset_impacts) || []);
        scanMacroGlossary();
      });
    }

    const snapGridEl = document.getElementById("macro-snapshot-grid");
    if (snapGridEl) {
      snapGridEl.addEventListener("click", (e) => {
        const card = e.target.closest ? e.target.closest(".macro-snap-card") : null;
        if (!card) return;
        openSnapshotModal(card.getAttribute("data-snap-id"));
      });
    }

    const glossaryOpenBtn = document.getElementById("macro-glossary-open");
    if (glossaryOpenBtn) glossaryOpenBtn.addEventListener("click", openGlossaryModal);

    if (window.SIGNAL_DESK_GLOSSARY && typeof window.SIGNAL_DESK_GLOSSARY.initToggle === "function") {
      window.SIGNAL_DESK_GLOSSARY.initToggle("glossary-toggle-btn", scanMacroGlossary);
    }

    const rangeTabs = document.getElementById("macro-cal-range-tabs");
    if (rangeTabs) {
      rangeTabs.addEventListener("click", (e) => {
        const btn = e.target.closest ? e.target.closest(".macro-seg-btn") : null;
        if (!btn) return;
        calRange = btn.getAttribute("data-range") || "week2";
        rangeTabs.querySelectorAll(".macro-seg-btn").forEach((b) => b.classList.toggle("active", b === btn));
        if (cachedMacro) {
          renderCalendar();
          tickCountdowns();
        }
      });
    }

    const starsSel = document.getElementById("macro-cal-stars");
    if (starsSel) {
      starsSel.addEventListener("change", () => {
        calStars = starsSel.value || "3";
        if (cachedMacro) {
          renderCalendar();
          tickCountdowns();
        }
      });
    }

    const indSel = document.getElementById("macro-cal-indicator");
    if (indSel) {
      indSel.addEventListener("change", () => {
        calIndicator = indSel.value || "all";
        if (cachedMacro) {
          renderCalendar();
          tickCountdowns();
        }
      });
    }

    const monthSel = document.getElementById("macro-cal-past-month");
    if (monthSel) {
      monthSel.addEventListener("change", () => {
        calPastMonth = monthSel.value || "recent7";
        if (cachedMacro) renderCalendar();
      });
    }

    const pastList = document.getElementById("macro-cal-past-list");
    if (pastList) {
      pastList.addEventListener("click", (e) => {
        const btn = e.target.closest ? e.target.closest(".macro-cal-past-summary") : null;
        if (!btn) return;
        const item = btn.closest(".macro-cal-past-item");
        if (!item) return;
        const pid = item.getAttribute("data-past-id");
        if (!pid) return;
        if (expandedPastIds.has(pid)) expandedPastIds.delete(pid);
        else expandedPastIds.add(pid);
        renderCalendar();
      });
    }

    document.addEventListener("click", (e) => {
      const histBtn = e.target.closest ? e.target.closest(".macro-hist-open") : null;
      if (histBtn) {
        openHistModal(histBtn.getAttribute("data-indicator-id"));
        return;
      }
      const icsBtn = e.target.closest ? e.target.closest(".macro-ics-btn") : null;
      if (icsBtn) {
        downloadIcs(
          icsBtn.getAttribute("data-ics-title") || "",
          icsBtn.getAttribute("data-ics-start") || "",
          icsBtn.getAttribute("data-ics-desc") || ""
        );
      }
    });

    const backdrop = document.getElementById("macro-hist-modal-backdrop");
    const closeBtn = document.getElementById("macro-hist-modal-close");
    if (closeBtn) closeBtn.addEventListener("click", closeHistModal);
    if (backdrop) {
      backdrop.addEventListener("click", (e) => {
        if (e.target === backdrop) closeHistModal();
      });
    }
    document.addEventListener("keydown", (e) => {
      if (backdrop && !backdrop.classList.contains("hidden") && (e.key === "Escape" || e.key === "Esc")) {
        closeHistModal();
      }
    });
  }

  function applyInitialHash() {
    const h = (window.location && window.location.hash ? window.location.hash.replace("#", "") : "") || "overview";
    if (["overview", "data", "calendar"].includes(h)) switchMacroTab(h);
  }

  window.__MACRO_DESK_UTILS__ = {
    getTaipeiWeekBounds,
    filterUpcomingEvents,
    filterPastEvents,
    getAvailablePastMonths,
    formatCountdown,
    renderIndicatorCardHtml,
    buildHistModalBodyHtml,
    renderAssetImpacts,
    renderMarketSnapshot,
    renderFedWatch,
    openSnapshotModal,
    openGlossaryModal,
    switchMacroTab,
    openHistModal,
    closeHistModal,
    setMacroDataForTest(doc) {
      cachedMacro = doc;
      renderAll();
    },
    setCalendarStateForTest(state) {
      if (state.range !== undefined) calRange = state.range;
      if (state.stars !== undefined) calStars = String(state.stars);
      if (state.indicator !== undefined) calIndicator = state.indicator;
      if (state.pastMonth !== undefined) calPastMonth = state.pastMonth;
      if (cachedMacro) renderCalendar();
    },
    getCalendarDefaults() {
      return { calRange, calStars, calIndicator, calPastMonth };
    },
  };

  bindEvents();
  applyInitialHash();
  refreshMacro();
  setInterval(tickCountdowns, 1000);
})();
