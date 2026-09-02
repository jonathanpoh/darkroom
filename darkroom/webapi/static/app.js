/* darkroom/webapi/static/app.js
 *
 * Transplanted from docs/design/safelight-mock.html (approved design mock).
 * Renders the two catalog views (targets overview, target detail) client-side
 * from a server-embedded `DATA` array and `STATES` (the ordered processed
 * states, names.PROCESSED_STATES) — see catalog/index.html and
 * catalog/target.html.
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

/* hardcoded — home first, then increasing distance from home (Azores last).
   Sites don't change often enough to warrant computing this from lat/lon. */
const SITE_ORDER = [
  "Home (Palmela)",
  "Quinta do Lago (Azeitão)",
  "Santa Susana",
  "São Cristóvão",
  "Sorte Verde",
  "Cais do Pico (Pico Island, Azores)",
  "Mount Pico (Pico Island, Azores)",
];
const siteRank = s => { const i = SITE_ORDER.indexOf(s); return i === -1 ? SITE_ORDER.length : i; };

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
  // Grow factors are each filter's *share* of the strip, scaled to sum to 100 —
  // not the raw hours. Raw hours as flex-grow silently under-fills whenever the
  // total is below 1h: flex distributes only `sum(grow)` of the free space when
  // that sum is < 1, so a 0.95h rig drew a bar 95% of its cell and looked
  // misaligned against every rig beside it.
  const segs = Object.entries(hours).sort((a, b) => a[0].localeCompare(b[0])).map(([f, h]) => {
    const grow = total > 0 ? (h / total) * 100 : 0;
    return `<div class="seg" data-tip="<b>${fname(f)}</b> · ${h.toFixed(1)}h" style="flex:${grow} 1 0; background:${fcolor(f)}"></div>`;
  }).join("");
  return `<div class="strip" style="width:${Math.max(pct, 4)}%">${segs}</div>`;
}

/* doneness gauge: how much integration is banked (per rig, or per target in
   the overview). sqrt scale, 30h = full.

   One table for the ladder: [upper bound, word, colour, modifier, range label].
   The footnotes describing it are generated from `zoneLadder()` below rather
   than written out by hand — they used to say "5–10h workable" and omit "solid"
   entirely, neither of which was ever true of these thresholds. */
const GAUGE_MAX = 30;
const ZONES = [
  [2,        "needs data", "var(--ink-3)",     "",     "under 2h"],
  [10,       "workable",   "var(--ink-2)",     "",     "2–10h"],
  [20,       "solid",      "var(--ink)",       "",     "10–20h"],
  [Infinity, "deep",       "var(--safelight)", "deep", "20h+"],
];
const zoneOf = h => ZONES.find(([max]) => h < max);
const zoneLadder = () => ZONES.map(([, word, , , range]) => `${range} ${word}`).join(" · ");

/* `showNumbers` is off wherever the hours are already on the row (the target
   page's rig summary, since hoursHTML puts them there); on the overview the
   tooltip is the only place the weighted figure appears, so it keeps them.

   `panels` > 1 means `h` is already a per-panel figure (see perPanel) — the
   gauge reads the same way, but the tooltip has to say so, or "2.1h workable"
   on an 8.4h mosaic looks like a bug rather than the point. */
function gaugeHTML(h, withWord = true, rawH, showNumbers = true, panels = 0) {
  const [, word, colour, mod, range] = zoneOf(h);
  const w = Math.min(Math.sqrt(h / GAUGE_MAX), 1) * 100;
  const tick = v => `<i class="gtick" style="left:${Math.sqrt(v / GAUGE_MAX) * 100}%"></i>`;
  const weighted = rawH !== undefined && Math.abs(h - rawH) > 0.05;
  const per = panels > 1 ? ` <i>per panel, across ${panels} panels</i>` : "";
  const tip = !showNumbers
    ? `<b>${word}</b> — ${range}${per}`
    : weighted
      ? `<b>${h.toFixed(1)}h</b> home-equivalent (${rawH.toFixed(1)}h raw) — ${word}${per}`
      : `<b>${h.toFixed(1)}h</b> — ${word}${per}`;
  return `<span class="gauge" data-tip="${tip}">
    <span class="gtrack"><span class="gfill" style="width:${w}%; background:${colour}"></span>${tick(2)}${tick(10)}${tick(20)}</span>
    ${withWord ? `<span class="gword ${mod}">${word}</span>` : ""}</span>`;
}

/* ── mosaic panels (M1) ────────────────────────
   A mosaic's hours are panel-time, not depth: 8.4h spread over four panels is
   a 2.1h-deep mosaic, and each panel is stacked and finished on its own (WBPP
   merges panels at final integration and fails, so one tree per panel). So
   every number that answers "does this need another night?" — the depth gauge
   on the overview, the per-rig gauge on the detail page — divides by the panel
   count, and the panel breakdown shows which panels are actually short.

   Sessions with no panel are ordinary single-pointing sessions and the
   overwhelming majority; for them panelCount is 0 and nothing below changes
   what the page used to show. */
const cmpPanel = (a, b) => {
  const [ar, ac] = a.split("-").map(Number), [br, bc] = b.split("-").map(Number);
  return (ar - br) || (ac - bc) || a.localeCompare(b);
};
const panelsOf = nights => [...new Set(nights.map(n => n.panel).filter(Boolean))].sort(cmpPanel);
const panelCount = nights => panelsOf(nights).length;
/* Divide by the panel count only when there is more than one panel — a single
   panel is just a session that happens to carry a label. Unpanelled hours
   inside a mosaic target (the odd pre-M1 row) ride along in the numerator;
   they are listed separately in the breakdown rather than silently dropped. */
const perPanel = (h, np) => (np > 1 ? h / np : h);

/* Hours, weighted-first: a night under a dark site banks more usable depth than
   the clock says, and that — not the raw figure — is what decides whether the
   target needs another session. The raw hours stay alongside in the dimmer grey,
   and only when the site's sky quality actually moves the number (same 0.05h
   threshold the gauge uses), so home sessions still read as a bare "5.5h". */
function hoursHTML(rawH, weightedH, cls = "h") {
  const wh = weightedH ?? rawH;
  const raw = Math.abs(wh - rawH) > 0.05 ? ` <span class="rawh">(${rawH.toFixed(1)}h raw)</span>` : "";
  return `<span class="${cls}"><b>${wh.toFixed(1)}</b>h${raw}</span>`;
}

const nameCell = (t) => {
  const np = t.n_panels || 0;
  const badge = np > 1 ? `<span class="pcount">${np}-panel mosaic</span>` : "";
  return `<span><span class="tname display">${t.target}</span>${t.cname ? `<span class="cname">${t.cname}</span>` : ""}${badge}</span>`;
};

/* ── calibration match (F3) ────────────────────
   One letter-chip per calibration class, coloured by whether `wbpp` would find
   a matching set. Server-computed (darkroom.catalog.match_session_calibration)
   so the chips and the prep run agree; `detail` carries the why, via the shared
   data-tip tooltip. */
const CAL_CLASSES = [["darks", "D", "Darks"], ["flats", "F", "Flats"], ["flat_darks", "FD", "Flat darks"]];
/* data-tip is assigned with innerHTML and deliberately allows <b>, so anything
   interpolated from the catalog (set ids, folder paths, camera names) is escaped. */
const esc = s => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
                          .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
function calCell(cal) {
  if (!cal) return `<span class="caldots"></span>`;
  const chips = CAL_CLASSES.map(([key, abbr, title]) => {
    const c = cal[key] || { status: "unknown", label: "can't tell", detail: "not computed" };
    const head = esc(`${title}: ${c.label}`);
    return `<i class="caldot ${c.status}" data-tip="<b>${head}</b> · ${esc(c.detail)}">${abbr}</i>`;
  }).join("");
  return `<span class="caldots">${chips}</span>`;
}

/* ── guiding (F4) ──────────────────────────────
   Total guide RMS in arcsec for the night, from the guide logs matched to this
   session by time (darkroom catalog scan-guiding). Absent for most sessions —
   the logs only cover part of the archive's history — and an em-dash is the
   honest answer there: no row means "not measured", never "guided badly".

   The verdicts — `band` (good/fair/poor), `partial` coverage, `spike` — are
   computed server-side (darkroom.webapi.ui) so this chip and the session
   page never disagree; this only renders them. */
function guidingCell(g) {
  if (!g || g.rms == null) return `<span class="guide none">—</span>`;
  const as = v => v == null ? "?" : v.toFixed(2) + "″";
  const bits = [
    `<b>${as(g.rms)} RMS</b> — ${g.band}`,
    `RA ${as(g.ra)} · Dec ${as(g.dec)}`,
    `peak ${as(g.peak)} · p95 ${as(g.p95)}`,
  ];
  if (g.cov != null) {
    const pct = (g.cov * 100).toFixed(0);
    /* A log covering a fraction of the night must not read as the verdict on
       all of it — say so rather than quietly showing the number. */
    bits.push(g.partial ? `coverage ${pct}% — partial log, not the whole night`
                      : `coverage ${pct}%`);
  }
  bits.push(`${g.frames ?? "?"} frames · ${g.lost ?? 0} star-loss · ${g.dropped ?? 0} dropped`);
  /* rms >= 2 * p95: the number is real, but it is a few wrecked subs rather
     than a bad night. Keep the value and its band, mark it. */
  if (g.spike) bits.push(`spike-dominated: most frames near ${as(g.p95)}, worst ${as(g.peak)} — a few bad subs rather than a bad night`);
  if (g.logs && g.logs.length) bits.push(esc(g.logs.join(", ")));
  const mark = g.spike ? `<span class="spike" aria-label="spike-dominated">▲</span>` : "";
  return `<span class="guide ${g.band}${g.partial ? " partial" : ""}" data-tip="${bits.join(" · ")}">${as(g.rms)}${mark}</span>`;
}

const backlogNights = t => t.nights.filter(n => n.state === "unprocessed" || n.state === "in_progress");
const backlogH = t => backlogNights(t).reduce((a, n) => a + n.h, 0);
const backlogWH = t => backlogNights(t).reduce((a, n) => a + (n.wh ?? n.h), 0);
const recountStates = t => { t.states = {}; t.nights.forEach(n => t.states[n.state] = (t.states[n.state] || 0) + 1); };

/* ── overview ──────────────────────────────── */
const OV_SORTS = {
  target: (a, b) => a.target.localeCompare(b.target),
  total:  (a, b) => a.total_h - b.total_h,
  open:   (a, b) => backlogH(a) - backlogH(b),
  n:      (a, b) => a.n - b.n,
  latest: (a, b) => (a.last || "").localeCompare(b.last || ""),
};
let ovSort = { key: "latest", desc: true }, query = "", catSel = "", filtSel = "", siteSel = "";

/* `attrs` carries any extra data-* the click handler needs (the rig, on the
   detail page's per-rig headers). */
function sortHead(key, label, current, extra="", attrs="") {
  const on = current.key === key;
  const arrow = on ? `<span class="dir">${current.desc ? "▼" : "▲"}</span>` : "";
  return `<button class="colhead sortable ${on ? "sorted" : ""} ${extra}" ${attrs} data-key="${key}">${label} ${arrow}</button>`;
}

function renderOverview() {
  const maxH = Math.max(...DATA.map(t => t.total_h));
  const allFilters = [...new Set(DATA.flatMap(t => t.nights.map(n => n.filter || "None")))].sort();
  const allSites = [...new Set(DATA.flatMap(t => t.nights.map(n => n.site).filter(Boolean)))]
    .sort((a, b) => siteRank(a) - siteRank(b) || a.localeCompare(b));
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
      const np = t.n_panels || 0;
      return `<a class="row cols" href="/targets/${encodeURIComponent(t.target)}">
        ${nameCell(t)}
        ${stripHTML(t.hours, maxH)}
        ${gaugeHTML(perPanel(backlogWH(t), np), false, perPanel(open, np), true, np)}
        <span class="hnum num"><b>${t.total_h.toFixed(1)}</b>h</span>
        <span class="opennum num ${open > 0 ? "some" : ""}">${open > 0 ? open.toFixed(1) + "h" : "—"}</span>
        <span class="marks">${counts}</span>
        <span class="lastn ${t.last >= "2026-06-01" ? "recent" : ""}">${t.last}</span>
      </a>`;
    }).join("");
  const badFilters = DATA.reduce((a, t) => a + t.nights.filter(n => !n.filter || n.filter === "None" || /_\d-\d/.test(n.filter)).length, 0);
  const badTargets = DATA.filter(t => /_\d-\d|M 82 M 82/.test(t.target)).length;
  const mosaics = DATA.filter(t => (t.n_panels || 0) > 1).length;
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
      <a class="go" href="/queue">→ cleanup queue</a></div>
    <p class="footnote">
      Open = hours in sessions still open or in progress ·
      Depth = open hours: ${zoneLadder()} — <b>per panel</b> on a mosaic, since 8h across 4 panels is a 2h-deep mosaic ·
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
    const mos = mosaics ? ` · <b>${mosaics}</b> mosaic${mosaics === 1 ? "" : "s"}` : "";
    statlineEl.innerHTML = `<b>${DATA.length}</b> targets · <b>${totalN}</b> sessions · <b>${totalH.toFixed(0)}h</b> integration${mos}`;
  }
}

/* ── detail: nights grouped by rig, expanded by default ── */
let detail = null;
const NIGHT_SORTS = {
  date:  (a, b) => (a.date || "").localeCompare(b.date || ""),
  state: (a, b) => STATES.indexOf(a.state) - STATES.indexOf(b.state),
  h:     (a, b) => a.h - b.h,
  /* Panel sorts numerically ("2-1" before "10-1"), unpanelled rows last. */
  panel: (a, b) => (!a.panel || !b.panel) ? (a.panel ? -1 : b.panel ? 1 : 0)
                                          : cmpPanel(a.panel, b.panel),
};

/* Per-panel breakdown for a mosaic target: one cell per panel, deepest-scaled
   gauge, so the panel that still needs a night is visible at a glance rather
   than buried inside a total. Panels accumulate independently across nights,
   and each is stacked on its own, so this — not the target total — is the
   thing to read before deciding where to point next.

   `short` marks a panel under three-quarters of the deepest one: the mosaic is
   only as finished as its thinnest tile. Any unpanelled sessions under the same
   target (a pre-M1 row, or a session genuinely shot as a single pointing) get
   their own cell rather than being folded into a panel's figure. */
function panelBlockHTML(t) {
  const labels = panelsOf(t.nights);
  if (labels.length < 2) return "";
  const hOf = ns => ns.reduce((a, n) => a + n.h, 0);
  const whOf = ns => ns.reduce((a, n) => a + (n.wh ?? n.h), 0);
  const bucket = p => t.nights.filter(n => (n.panel || null) === p);
  const deepest = Math.max(...labels.map(p => whOf(bucket(p))));
  const cell = (label, nights, cls = "", tip = "") => {
    const wh = whOf(nights), h = hOf(nights);
    return `<div class="pcell ${cls}"${tip ? ` data-tip="${tip}"` : ""}>
      <span class="plabel">${label}</span>
      ${gaugeHTML(wh, false, h)}
      ${hoursHTML(h, wh, "hnum num")}
      <span class="n">${nights.length} night${nights.length === 1 ? "" : "s"}</span>
    </div>`;
  };
  const cells = labels.map(p => {
    const ns = bucket(p);
    const short = whOf(ns) < 0.75 * deepest;
    return cell(`P${p}`, ns, short ? "short" : "",
      short ? `<b>panel ${p} is short</b> · ${whOf(ns).toFixed(1)}h against ${deepest.toFixed(1)}h on the deepest panel` : "");
  }).join("");
  const loose = bucket(null);
  const total = hOf(t.nights);
  return `<div class="panelblock">
    <div class="pblockhead">
      <span class="ptitle">Panels</span>
      <span class="sub">${labels.length} panels · ${(total / labels.length).toFixed(1)}h per panel
        · ${total.toFixed(1)}h of panel-time in total</span>
    </div>
    <div class="pgrid">${cells}${loose.length ? cell("unpanelled", loose, "loose",
      "<b>no panel label</b> · a single pointing under this target, or a row ingested before panels existed") : ""}</div>
  </div>`;
}

function renderDetail() {
  const t = DATA.find(x => x.target === detail.name);
  const np = t.n_panels || 0;
  const rigs = {};
  t.nights.forEach(n => { const r = `${n.ota || "?"} · ${n.camera || "?"}`; (rigs[r] = rigs[r] || []).push(n); });

  const groups = Object.entries(rigs).sort((a, b) => b[1].length - a[1].length).map(([rig, nights]) => {
    const gsort = detail.sorts[rig] || { key: "date", desc: true };
    const sorted = [...nights].sort((a, b) => (gsort.desc ? -1 : 1) * NIGHT_SORTS[gsort.key](a, b));
    const gh = nights.reduce((a, n) => a + n.h, 0);
    const ghw = nights.reduce((a, n) => a + (n.wh ?? n.h), 0);
    /* Panels within this rig, not the target's total: a mosaic can be half
       shot on one rig and half on another, and each rig's gauge answers only
       for the nights under it. */
    const gnp = panelCount(nights);
    const rows = sorted.map(n => `
      <div class="row cols nightcols${np > 1 ? " panelled" : ""} night">
        <button class="markbtn" data-sid="${n.sid}" title="${STATE_LABEL[n.state]} — click to cycle">${markSVG(n.state, n.sid)}</button>
        <span class="date"><a href="/sessions/${encodeURIComponent(n.sid)}">${n.date}</a></span>
        ${np > 1 ? `<span class="pchip${n.panel ? "" : " none"}">${n.panel ? "P" + n.panel : "—"}</span>` : ""}
        <span class="fchip"><i style="background:${fcolor(n.filter || "None")}"></i>${fname(n.filter || "None")}</span>
        <span class="exp">${n.frames || "?"} × ${n.exp ? n.exp.toFixed(0) + "s" : "?"}${n.gain ? " · gain" + n.gain : ""}</span>
        <span class="statelabel ${n.state}">${STATE_LABEL[n.state]}</span>
        <span class="sitecell"><span class="sitechip">${n.site || ""}</span>${n.w !== undefined && n.w !== 1 ? `<span class="wbadge">×${n.w}</span>` : ""}</span>
        ${calCell(n.cal)}
        ${guidingCell(n.guiding)}
        ${hoursHTML(n.h, n.wh)}
      </div>`).join("");
    const gs = (key, label, extra="") => sortHead(key, label, gsort, extra, `data-rig="${rig}"`);
    return `<details class="rig" data-rig="${rig}" ${detail.closed.has(rig) ? "" : "open"}>
      <summary class="rigsum">
        <svg class="tri" width="10" height="10" viewBox="0 0 10 10" aria-hidden="true"><path d="M2.5 1 L8 5 L2.5 9 Z" fill="currentColor"/></svg>
        <span class="rigname display">${rig}</span>
        ${gaugeHTML(perPanel(ghw, gnp), true, perPanel(gh, gnp), false, gnp)}
        ${stripHTML(hoursOf(nights), null)}
        ${hoursHTML(gh, ghw, "hnum num")}
        <span class="n">${nights.length} sessions${gnp > 1 ? ` · ${gnp} panels` : ""}</span>
      </summary>
      <div class="rigbody">
        <div class="cols nightcols${np > 1 ? " panelled" : ""} headrow">
          <span class="colhead"></span>${gs("date", "Night")}${np > 1 ? gs("panel", "Panel") : ""}<span class="colhead">Filter</span>
          <span class="colhead">Exposure</span>${gs("state", "State")}<span class="colhead">Site</span><span class="colhead">Cal</span><span class="colhead">Guiding</span>${gs("h", "Hours", "num")}
        </div>
        ${rows}
      </div>
    </details>`;
  }).join("");

  document.getElementById("app").innerHTML = `
    <a class="backlink" href="/">← all targets</a>
    <div class="dethead">
      ${nameCell(t)}
      <span class="sub">${t.n} sessions · <b style="color:var(--ink)">${t.total_h.toFixed(1)}h</b>${
        np > 1 ? ` panel-time · <b style="color:var(--ink)">${perPanel(t.total_h, np).toFixed(1)}h</b> per panel` : ""
      } · last acquired ${t.last}</span>
      ${stripHTML(t.hours, null)}
    </div>
    ${panelBlockHTML(t)}
    ${groups}
    <p class="footnote">grease-pencil marks: <span class="lamp">○</span> processed · half-circle in progress · strike skipped · dotted = open.
      click a mark to cycle state — updates the catalog.
      gauge = integration banked per rig: ${zoneLadder()}, weighted by site sky quality${np > 1 ? ", and per panel — a mosaic is only as deep as each tile, which is stacked and finished on its own" : ""}.
      Hours are home-equivalent — what the integration would have been worth from home — with the raw figure alongside when the site's sky quality moves it.
      Site column: named observing site the session's coordinates matched, if any; a ×badge shows its SQM weight relative to home when it isn't 1×.
      Cal: what <b>wbpp</b> would find for Darks / Flats / FlatDarks — lit = matched, dim = missing, faded = that camera doesn't use them, dashed = can't tell. Hover for the matched set. Catalog only: this server can't see the archive.
      Guiding: total guide RMS from the PHD2 logs matched to the night by time — under 1″ good, 1–2″ fair, over 2″ poor; an em-dash means no log covers it, not bad guiding. Hover for RA/Dec, peak, p95 and how much of the night the log actually covers.</p>`;
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
    recountStates(t);
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
      recountStates(t);
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
