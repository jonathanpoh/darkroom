/* darkroom/webapi/static/app.js
 *
 * Transplanted from docs/design/safelight-mock.html (approved design mock).
 * Renders the two catalog views (targets overview, target detail) client-side
 * from a server-embedded `DATA` array (see catalog/index.html and
 * catalog/target.html).
 *
 * Differences from the mock (real app vs. interactive mock):
 *   - COMMON_NAMES lookup is gone; the server embeds `cname` on each target
 *     row instead (darkroom.webapi.common_names).
 *   - Overview rows are real links to /targets/<target> (server-rendered
 *     detail route), not client-side view swaps.
 *   - The detail "back" link is a real link to /, not a client-side reset.
 *   - Mark clicks POST to the existing /sessions/{id}/state endpoint
 *     (optimistic update, revert + alert on failure) instead of only
 *     mutating in-memory state.
 */

const FILTER_COLOR = {
  "BaaderNeodymium": "var(--f-baader)",
  "L-Extreme": "var(--f-extreme)",
  "L-Pro": "var(--f-lpro)",
  "L-Synergy": "var(--f-synergy)",
};
const fcolor = f => FILTER_COLOR[f] || "var(--f-none)";
const fname = f => (f === "None" ? "no filter recorded" : f);

const CATALOGS = [
  ["M", "Messier", t => /^M \d/.test(t)],
  ["NGC", "NGC", t => /^NGC/.test(t)],
  ["IC", "IC", t => /^IC/.test(t)],
  ["Sh2", "Sharpless", t => /^Sh2/.test(t)],
  ["C", "Caldwell", t => /^C \d/.test(t)],
  ["LDN", "LDN", t => /^LDN/.test(t)],
  ["other", "other", t => !/^(M \d|NGC|IC|Sh2|C \d|LDN)/.test(t)],
];
const catalogOf = t => (CATALOGS.find(([, , fn]) => fn(t)) || ["other"])[0];

const STATES = ["unprocessed", "in_progress", "processed", "skipped"];
const STATE_LABEL = { unprocessed: "open", in_progress: "in progress", processed: "processed", skipped: "skipped" };

/* grease-pencil marks: the one hand element. deterministic tilt per session id. */
function tilt(sid) { let h = 0; for (const c of sid) h = (h * 31 + c.charCodeAt(0)) & 1023; return (h % 9) - 4; }
function markSVG(state, sid) {
  const rot = `transform="rotate(${tilt(sid)} 18 14)"`;
  const circle = `<path class="pencil" ${rot} d="M 6,15 C 4,7 13,2.5 20,3 C 28,3.5 32,8 30.5,15 C 29,22 20,25.5 12,24 C 6.5,23 5.5,19 7,15.5"/>`;
  const half   = `<path class="pencil" ${rot} d="M 6,17 C 4.5,9 12,3.5 19,3.5 C 25,3.5 29.5,6.5 31,11"/>`;
  const strike = `<path class="pencil" ${rot} d="M 5,21 C 13,17 23,10 31,5.5"/>`;
  const ghost  = `<ellipse class="ghost" cx="18" cy="14" rx="13" ry="10.5"/>`;
  const inner = state === "processed" ? circle : state === "in_progress" ? half : state === "skipped" ? strike : ghost;
  return `<svg width="36" height="28" viewBox="0 0 36 28" aria-hidden="true">${inner}</svg>`;
}
function miniMark(state) {
  const p = { processed: `<circle cx="7" cy="7" r="5.2" fill="none" stroke="var(--safelight)" stroke-width="1.8"/>`,
              in_progress: `<path d="M 2,9 A 5.2 5.2 0 0 1 12,6" fill="none" stroke="var(--ink-2)" stroke-width="1.8" stroke-linecap="round"/>`,
              unprocessed: `<circle cx="7" cy="7" r="5" fill="none" stroke="var(--ink-3)" stroke-width="1" stroke-dasharray="2 2.4"/>`,
              skipped: `<path d="M 2,11 L 12,3" stroke="var(--ink-3)" stroke-width="1.8" stroke-linecap="round"/>` };
  return `<svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true">${p[state]}</svg>`;
}

function hoursOf(nights) {
  const h = {};
  for (const n of nights) { const f = n.filter || "None"; h[f] = (h[f] || 0) + n.h; }
  return h;
}
function stripHTML(hours, totalMax) {
  const total = Object.values(hours).reduce((a, b) => a + b, 0);
  const pct = totalMax ? (total / totalMax) * 100 : 100;
  const segs = Object.entries(hours).sort((a, b) => a[0].localeCompare(b[0])).map(([f, h]) =>
    `<div class="seg" data-tip="<b>${fname(f)}</b> · ${h.toFixed(1)}h" style="flex:${h} ${h} 0; background:${fcolor(f)}"></div>`
  ).join("");
  return `<div class="strip" style="width:${Math.max(pct, 4)}%">${segs}</div>`;
}

/* doneness gauge: how much integration is banked (per rig, or per target in
   the overview). <2h needs more data · 5–10h workable · 20h+ good.
   sqrt scale, 30h = full. */
const GAUGE_MAX = 30;
function gaugeHTML(h, withWord = true, rawH) {
  const zone = h < 2 ? ["needs data", "var(--ink-3)", ""] :
               h < 10 ? ["workable", "var(--ink-2)", ""] :
               h < 20 ? ["solid", "var(--ink)", ""] :
                        ["deep", "var(--safelight)", "deep"];
  const w = Math.min(Math.sqrt(h / GAUGE_MAX), 1) * 100;
  const tick = v => `<i class="gtick" style="left:${Math.sqrt(v / GAUGE_MAX) * 100}%"></i>`;
  const weighted = rawH !== undefined && Math.abs(h - rawH) > 0.05;
  const tip = weighted
    ? `<b>${h.toFixed(1)}h</b> home-equivalent (${rawH.toFixed(1)}h raw) — ${zone[0]}`
    : `<b>${h.toFixed(1)}h</b> — ${zone[0]}`;
  return `<span class="gauge" data-tip="${tip}">
    <span class="gtrack"><span class="gfill" style="width:${w}%; background:${zone[1]}"></span>${tick(2)}${tick(10)}${tick(20)}</span>
    ${withWord ? `<span class="gword ${zone[2]}">${zone[0]}</span>` : ""}</span>`;
}

const nameCell = (t) =>
  `<span><span class="tname display">${t.target}</span>${t.cname ? `<span class="cname">${t.cname}</span>` : ""}</span>`;

const backlogH = t => t.nights.filter(n => n.state === "unprocessed" || n.state === "in_progress")
                              .reduce((a, n) => a + n.h, 0);
const backlogWH = t => t.nights.filter(n => n.state === "unprocessed" || n.state === "in_progress")
                               .reduce((a, n) => a + (n.wh ?? n.h), 0);

/* ── overview ──────────────────────────────── */
const OV_SORTS = {
  target: (a, b) => a.target.localeCompare(b.target),
  total:  (a, b) => a.total_h - b.total_h,
  open:   (a, b) => backlogH(a) - backlogH(b),
  n:      (a, b) => a.n - b.n,
  latest: (a, b) => (a.last || "").localeCompare(b.last || ""),
};
let ovSort = { key: "latest", desc: true }, query = "", catSel = "", filtSel = "", siteSel = "";

function sortHead(key, label, current, extra="") {
  const on = current.key === key;
  const arrow = on ? `<span class="dir">${current.desc ? "▼" : "▲"}</span>` : "";
  return `<button class="colhead sortable ${on ? "sorted" : ""} ${extra}" data-key="${key}">${label} ${arrow}</button>`;
}

function renderOverview() {
  const maxH = Math.max(...DATA.map(t => t.total_h));
  const allFilters = [...new Set(DATA.flatMap(t => t.nights.map(n => n.filter || "None")))].sort();
  const allSites = [...new Set(DATA.flatMap(t => t.nights.map(n => n.site).filter(Boolean)))].sort();
  const visible = DATA
    .filter(t => t.target.toLowerCase().includes(query) ||
                 (t.cname || "").toLowerCase().includes(query))
    .filter(t => !catSel || catalogOf(t.target) === catSel)
    .filter(t => !filtSel || t.nights.some(n => (n.filter || "None") === filtSel))
    .filter(t => !siteSel || t.nights.some(n => n.site === siteSel));
  const rows = visible
    .sort((a, b) => (ovSort.desc ? -1 : 1) * OV_SORTS[ovSort.key](a, b))
    .map(t => {
      const counts = STATES.filter(s => t.states[s])
        .map(s => `<span title="${t.states[s]} ${STATE_LABEL[s]}">${miniMark(s)}${t.states[s]}</span>`).join("");
      const open = backlogH(t);
      return `<a class="row cols" href="/targets/${encodeURIComponent(t.target)}">
        ${nameCell(t)}
        ${stripHTML(t.hours, maxH)}
        ${gaugeHTML(backlogWH(t), false, backlogH(t))}
        <span class="hnum num"><b>${t.total_h.toFixed(1)}</b>h</span>
        <span class="opennum num ${open > 0 ? "some" : ""}">${open > 0 ? open.toFixed(1) + "h" : "—"}</span>
        <span class="marks">${counts}</span>
        <span class="lastn ${t.last >= "2026-06-01" ? "recent" : ""}">${t.last}</span>
      </a>`;
    }).join("");
  const badFilters = DATA.reduce((a, t) => a + t.nights.filter(n => !n.filter || n.filter === "None" || /_\d-\d/.test(n.filter)).length, 0);
  const badTargets = DATA.filter(t => /_\d-\d|M 82 M 82/.test(t.target)).length;
  document.getElementById("app").innerHTML = `
    <div class="controls">
      <input type="search" placeholder="find a target…" value="${query}" id="q">
      <select id="catsel" class="${catSel ? "active" : ""}" aria-label="Filter by catalog">
        <option value="">all catalogs</option>
        ${CATALOGS.map(([k, label]) => `<option value="${k}" ${catSel === k ? "selected" : ""}>${label}</option>`).join("")}
      </select>
      <select id="filtsel" class="${filtSel ? "active" : ""}" aria-label="Filter by optical filter">
        <option value="">any filter</option>
        ${allFilters.map(f => `<option value="${f}" ${filtSel === f ? "selected" : ""}>${fname(f)}</option>`).join("")}
      </select>
      <select id="sitesel" class="${siteSel ? "active" : ""}" aria-label="Filter by imaging site">
        <option value="">any site</option>
        ${allSites.map(s => `<option value="${s}" ${siteSel === s ? "selected" : ""}>${s}</option>`).join("")}
      </select>
      <div class="legend">
        <span><i style="background:var(--f-lpro)"></i>L-Pro</span>
        <span><i style="background:var(--f-extreme)"></i>L-Extreme</span>
        <span><i style="background:var(--f-synergy)"></i>L-Synergy</span>
        <span><i style="background:var(--f-baader)"></i>Baader</span>
        <span><i style="background:var(--f-none)"></i>none / other</span>
      </div>
    </div>
    <div class="cols headrow">
      ${sortHead("target", "Target", ovSort)}
      <span class="colhead">Integration by filter</span>
      <span class="colhead">Depth</span>
      ${sortHead("total", "Total", ovSort, "num")}
      ${sortHead("open", "Open", ovSort, "num")}
      ${sortHead("n", "Sessions", ovSort)}
      ${sortHead("latest", "Latest", ovSort, "num")}
    </div>
    ${rows || `<p style="color:var(--ink-3); padding:20px 10px">No targets match. Clear a filter above.</p>`}
    <div class="cleanup"><b>${badFilters} sessions</b> have a missing or suspect filter · <b>${badTargets} targets</b> look like mosaic panels or duplicated names
      <span class="go">→ cleanup queue (coming soon)</span></div>
    <p class="footnote">
      Open = hours in sessions still open or in progress ·
      Depth = open hours: &lt;2h needs data · 5–10h workable · 20h+ deep ·
      marks are clickable in the target view ·
      Depth is weighted by site sky quality (SQM flux ratio) when known — home-equivalent hours</p>`;
  document.getElementById("q").addEventListener("input", e => { query = e.target.value.toLowerCase(); renderOverview(); const q = document.getElementById("q"); q.focus(); q.setSelectionRange(q.value.length, q.value.length); });
  document.getElementById("catsel").addEventListener("change", e => { catSel = e.target.value; renderOverview(); });
  document.getElementById("filtsel").addEventListener("change", e => { filtSel = e.target.value; renderOverview(); });
  document.getElementById("sitesel").addEventListener("change", e => { siteSel = e.target.value; renderOverview(); });
  document.querySelectorAll(".colhead.sortable").forEach(h => h.addEventListener("click", () => {
    const k = h.dataset.key;
    ovSort = { key: k, desc: ovSort.key === k ? !ovSort.desc : true };
    renderOverview();
  }));
  const statlineEl = document.getElementById("statline");
  if (statlineEl) {
    const totalH = DATA.reduce((a, t) => a + t.total_h, 0);
    const totalN = DATA.reduce((a, t) => a + t.n, 0);
    statlineEl.innerHTML = `<b>${DATA.length}</b> targets · <b>${totalN}</b> sessions · <b>${totalH.toFixed(0)}h</b> integration`;
  }
}

/* ── detail: nights grouped by rig, expanded by default ── */
let detail = null;
const NIGHT_SORTS = {
  date:  (a, b) => (a.date || "").localeCompare(b.date || ""),
  state: (a, b) => STATES.indexOf(a.state) - STATES.indexOf(b.state),
  h:     (a, b) => a.h - b.h,
};

function renderDetail() {
  const t = DATA.find(x => x.target === detail.name);
  const rigs = {};
  t.nights.forEach(n => { const r = `${n.ota || "?"} · ${n.camera || "?"}`; (rigs[r] = rigs[r] || []).push(n); });

  const groups = Object.entries(rigs).sort((a, b) => b[1].length - a[1].length).map(([rig, nights]) => {
    const gsort = detail.sorts[rig] || { key: "date", desc: true };
    const sorted = [...nights].sort((a, b) => (gsort.desc ? -1 : 1) * NIGHT_SORTS[gsort.key](a, b));
    const gh = nights.reduce((a, n) => a + n.h, 0);
    const ghw = nights.reduce((a, n) => a + (n.wh ?? n.h), 0);
    const rows = sorted.map(n => `
      <div class="row cols nightcols night">
        <button class="markbtn" data-sid="${n.sid}" title="${STATE_LABEL[n.state]} — click to cycle">${markSVG(n.state, n.sid)}</button>
        <span class="date"><a href="/sessions/${encodeURIComponent(n.sid)}">${n.date}</a></span>
        <span class="fchip"><i style="background:${fcolor(n.filter || "None")}"></i>${fname(n.filter || "None")}</span>
        <span class="exp">${n.frames || "?"} × ${n.exp ? n.exp.toFixed(0) + "s" : "?"}${n.gain ? " · gain" + n.gain : ""}</span>
        <span class="statelabel ${n.state}">${STATE_LABEL[n.state]}</span>
        <span class="sitecell"><span class="sitechip">${n.site || ""}</span>${n.w !== undefined && n.w !== 1 ? `<span class="wbadge">×${n.w}</span>` : ""}</span>
        <span class="h">${n.h.toFixed(1)}h</span>
      </div>`).join("");
    const gs = (key, label, extra="") => {
      const on = gsort.key === key;
      return `<button class="colhead sortable ${on ? "sorted" : ""} ${extra}" data-rig="${rig}" data-key="${key}">${label} ${on ? `<span class="dir">${gsort.desc ? "▼" : "▲"}</span>` : ""}</button>`;
    };
    return `<details class="rig" data-rig="${rig}" ${detail.closed.has(rig) ? "" : "open"}>
      <summary class="rigsum">
        <svg class="tri" width="10" height="10" viewBox="0 0 10 10" aria-hidden="true"><path d="M2.5 1 L8 5 L2.5 9 Z" fill="currentColor"/></svg>
        <span class="rigname display">${rig}</span>
        ${gaugeHTML(ghw, true, gh)}
        ${stripHTML(hoursOf(nights), null)}
        <span class="hnum num"><b>${gh.toFixed(1)}</b>h</span>
        <span class="n">${nights.length} sessions</span>
      </summary>
      <div class="rigbody">
        <div class="cols nightcols headrow">
          <span class="colhead"></span>${gs("date", "Night")}<span class="colhead">Filter</span>
          <span class="colhead">Exposure</span>${gs("state", "State")}<span class="colhead">Site</span>${gs("h", "Hours", "num")}
        </div>
        ${rows}
      </div>
    </details>`;
  }).join("");

  document.getElementById("app").innerHTML = `
    <a class="backlink" href="/">← all targets</a>
    <div class="dethead">
      ${nameCell(t)}
      <span class="sub">${t.n} sessions · <b style="color:var(--ink)">${t.total_h.toFixed(1)}h</b> · last acquired ${t.last}</span>
      ${stripHTML(t.hours, null)}
    </div>
    ${groups}
    <p class="footnote">grease-pencil marks: <span class="lamp">○</span> processed · half-circle in progress · strike skipped · dotted = open.
      click a mark to cycle state — updates the catalog.
      gauge = integration banked per rig: &lt;2h needs data · 5–10h workable · 20h+ deep, weighted by site sky quality.
      Site column: named observing site the session's coordinates matched, if any; a ×badge shows its SQM weight relative to home when it isn't 1×.</p>`;
  document.querySelectorAll("details.rig").forEach(d => d.addEventListener("toggle", () => {
    if (d.open) detail.closed.delete(d.dataset.rig); else detail.closed.add(d.dataset.rig);
  }));
  document.querySelectorAll(".rigbody .colhead.sortable").forEach(h => h.addEventListener("click", () => {
    const rig = h.dataset.rig, k = h.dataset.key, cur = detail.sorts[rig] || { key: "date", desc: true };
    detail.sorts[rig] = { key: k, desc: cur.key === k ? !cur.desc : true };
    renderDetail();
  }));
  document.querySelectorAll(".markbtn").forEach(b => b.addEventListener("click", e => {
    e.preventDefault();
    const n = t.nights.find(x => x.sid === b.dataset.sid);
    const prevState = n.state;
    const nextState = STATES[(STATES.indexOf(n.state) + 1) % STATES.length];
    n.state = nextState;
    t.states = {}; t.nights.forEach(x => t.states[x.state] = (t.states[x.state] || 0) + 1);
    renderDetail();
    fetch(`/sessions/${encodeURIComponent(n.sid)}/state`, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ state: nextState, next: location.pathname }),
    }).then(resp => {
      const ok = resp.ok || resp.type === "opaqueredirect" || (resp.status >= 200 && resp.status < 400);
      if (!ok) throw new Error(`unexpected status ${resp.status}`);
    }).catch(() => {
      n.state = prevState;
      t.states = {}; t.nights.forEach(x => t.states[x.state] = (t.states[x.state] || 0) + 1);
      renderDetail();
      alert("Failed to update session state — reverted.");
    });
  }));
}

/* shared tooltip for strip segments + gauges */
const tip = document.getElementById("tip");
document.addEventListener("mousemove", e => {
  const seg = e.target.closest("[data-tip]");
  if (seg) { tip.innerHTML = seg.dataset.tip; tip.style.display = "block";
    tip.style.left = Math.min(e.clientX + 14, innerWidth - 260) + "px"; tip.style.top = (e.clientY + 16) + "px";
  } else tip.style.display = "none";
});

if (typeof DETAIL_TARGET !== "undefined") {
  detail = { name: DETAIL_TARGET, closed: new Set(), sorts: {} };
  renderDetail();
} else {
  renderOverview();
}
