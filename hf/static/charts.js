// RSOSTBTEST-pro leaderboard graphs for the static Space.
//
// A port of src/rsostb/leaderboard/views.py (graph data, methodology) and
// charts.py (layout and SVG), so the static page draws the same charts as
// the Gradio Space and the CLI. tests/test_static_charts.py runs this file
// under Node and checks its graph data against the Python implementation.
//
// Inlined into index.html by `rsostb space stage`; also loadable by Node
// (module.exports). It never touches the DOM: it returns data and SVG text,
// and every string it puts in SVG is escaped.
(function (root) {
  "use strict";

  const CHART_TYPES = ["bar", "column", "dot", "radar", "line", "scatter"];
  const MODES = ["one_vs_all", "selected"];
  const DIMENSIONS = { categories: "Categories", metrics: "Metrics", difficulty: "Difficulty", families: "Evaluation families" };
  const DIFFICULTY_ORDER = ["easy", "medium", "hard", "expert", "adversarial", "1", "2", "3", "4", "5", "6"];
  const LEVEL_NAMES = { 1: "Level 1 (easy)", 2: "Level 2 (medium)", 3: "Level 3 (hard)", 4: "Level 4 (expert)",
    5: "Level 5 (adversarial)", 6: "Level 6 (frontier)" };
  const SCATTER_MEASURES = { rsostb_score: "RSOSTB Score", normalized: "Normalised score", efficiency: "Efficiency E",
    tokens_per_second: "Output tokens/s", tokens_per_task: "Output tokens per task",
    parameters_b: "Parameters (billions)", coverage: "Coverage" };
  const SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"];
  const NEUTRAL = { median: "#898781", best: "#52514e", peer: "#b5b3ad" };
  const SURFACE = "#fcfcfb", TEXT = "#0b0b0b", TEXT2 = "#52514e", MUTED = "#6f6e69", GRID = "#e5e4e0";
  const FONT = "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif";
  const W = 880, MAX_SERIES = SERIES.length;

  // ------------------------------------------------------------------ versions and methodology
  const vkey = v => String(v).replace(/^v/, "").split(/[.\-]/).map(x => (/^\d+$/.test(x) ? Number(x) : x));
  function cmpKey(a, b) {
    const x = vkey(a), y = vkey(b);
    for (let i = 0; i < Math.max(x.length, y.length); i++) {
      if (x[i] === undefined) return -1;
      if (y[i] === undefined) return 1;
      if (x[i] < y[i]) return -1;
      if (x[i] > y[i]) return 1;
    }
    return 0;
  }
  const uniqSorted = xs => [...new Set(xs)].sort(cmpKey);
  const scoringVersions = es => uniqSorted(es.map(e => e.scoring_version));
  const benchmarkVersions = es => uniqSorted(es.map(e => e.benchmark_version));
  const methodologyOf = e => `benchmark ${e.benchmark_version} · scoring ${e.scoring_version}`;

  function methodology(es) {
    const sv = scoringVersions(es), bv = benchmarkVersions(es);
    const keys = new Set(es.map(e => e.compat_key));
    const notes = [];
    if (sv.length > 1) notes.push(`Different scoring methods (${sv.join(", ")}): the scores are on different scales and are not comparable. Compare entries scored the same way.`);
    if (bv.length > 1) notes.push(`Different benchmark versions (${bv.join(", ")}): different task sets; category and metric percentages are only roughly comparable.`);
    if (keys.size > 1 && sv.length <= 1 && bv.length <= 1) notes.push("Same benchmark and scoring version, but different judges or scoring configuration: not directly comparable.");
    return { comparable: keys.size <= 1, scoring_versions: sv, benchmark_versions: bv, notes,
      label: keys.size <= 1 ? "comparable" : (sv.length > 1 || bv.length > 1 ? "different methodologies" : "different configuration") };
  }

  const entryName = (e, withMethod) => {
    const name = `${e.model.name} [${e.submission_id.slice(0, 6)}]`;
    return withMethod ? `${name} (${methodologyOf(e)})` : name;
  };

  // ------------------------------------------------------------------ graph data
  function dimItems(e, dimension) {
    const s = e.scores;
    if (dimension === "categories") return { ...(s.categories || {}) };
    if (dimension === "metrics") return Object.fromEntries(Object.entries(s.metrics || {}).filter(([k]) => k !== "rsostb_score"));
    if (dimension === "difficulty") return { ...(s.difficulty || {}) };
    if (dimension === "families") return { ...(s.evaluation_families || {}) };
    throw new Error(`unknown dimension ${dimension}`);
  }

  function orderKeys(keys, dimension, order) {
    if (dimension === "difficulty") {
      return [...DIFFICULTY_ORDER.filter(k => keys.has(k)), ...[...keys].filter(k => !DIFFICULTY_ORDER.includes(k)).sort()];
    }
    const known = (order || []).filter(k => keys.has(k));
    return [...known, ...[...keys].filter(k => !known.includes(k)).sort()];
  }
  const dimLabel = (k, dimension, names) => (dimension === "difficulty" ? (LEVEL_NAMES[k] || k) : (names[k] || k));

  function parseParameters(v) {
    if (v === null || v === undefined || typeof v === "boolean") return null;
    if (typeof v === "number") return v > 1e5 ? v / 1e9 : v;
    const m = /(?:(\d+)\s*[x×]\s*)?(\d+(?:\.\d+)?)\s*([kmbt])(?![a-z])/i.exec(String(v));
    if (!m) return null;
    const unit = { k: 1e-6, m: 1e-3, b: 1, t: 1e3 }[m[3].toLowerCase()];
    return (m[1] ? Number(m[1]) : 1) * Number(m[2]) * unit;
  }

  function measure(e, key) {
    const s = e.scores, eff = e.efficiency || {};
    const nn = v => (v === undefined ? null : v);
    if (key === "rsostb_score") return nn(s.rsostb_score);
    if (key === "normalized") return nn(s.normalized);
    if (key === "efficiency") return nn(eff.normalized);
    if (key === "tokens_per_second") return nn(eff.output_tokens_per_second);
    if (key === "tokens_per_task") return nn(eff.output_tokens_per_task);
    if (key === "coverage") return nn(e.coverage);
    if (key === "parameters_b") return parseParameters(e.model.parameters) ?? parseParameters(e.model.name);
    if (key.startsWith("metric:")) return nn((s.metrics || {})[key.slice(7)]);
    if (key.startsWith("cat:")) return nn((s.categories || {})[key.slice(4)]);
    throw new Error(`unknown measure ${key}`);
  }
  const measureLabel = (key, names) => SCATTER_MEASURES[key] || (key.includes(":") ? `${names[key.split(":")[1]] || key.split(":")[1]} (%)` : key);

  function median(xs) {
    const s = [...xs].sort((a, b) => a - b), n = s.length;
    return n % 2 ? s[(n - 1) / 2] : (s[n / 2 - 1] + s[n / 2]) / 2;
  }

  function empty(mode, dimension, chart, why) {
    return { mode, chart, dimension, title: why, unit: null, dimensions: [], series: [], points: [], notes: [why], comparable: true, x_label: null, y_label: null };
  }

  // opts: {mode, dimension, chart, focus, selected, names, order: {categories: [...], metrics: [...]}, x, y}
  function graph(entries, opts) {
    const mode = opts.mode || "selected", dimension = opts.dimension || "categories", chart = opts.chart || "bar";
    const names = opts.names || {}, x = opts.x || "efficiency", y = opts.y || "rsostb_score";
    if (!MODES.includes(mode)) throw new Error(`unknown mode ${mode}`);
    if (!CHART_TYPES.includes(chart)) throw new Error(`unknown chart type ${chart}`);
    const byId = Object.fromEntries(entries.map(e => [e.submission_id, e]));
    const notes = [];
    let chosen, peers, method;
    if (mode === "one_vs_all") {
      const f = byId[opts.focus || ""];
      if (!f) return empty(mode, dimension, chart, "Pick an entry to compare with everyone else.");
      peers = entries.filter(e => e.submission_id !== f.submission_id && e.scoring_version === f.scoring_version && e.benchmark_version === f.benchmark_version);
      const others = entries.filter(e => e.submission_id !== f.submission_id).length - peers.length;
      notes.push(`Compared with ${peers.length} other entr${peers.length === 1 ? "y" : "ies"} scored the same way (${methodologyOf(f)}).`);
      if (others) notes.push(`${others} entr${others === 1 ? "y" : "ies"} scored another way ${others === 1 ? "is" : "are"} left out: different methodologies are not comparable.`);
      chosen = [f];
      method = methodology([f, ...peers]);
    } else {
      chosen = (opts.selected || []).filter(i => byId[i]).map(i => byId[i]);
      if (!chosen.length) return empty(mode, dimension, chart, "Pick one or more entries.");
      peers = [];
      method = methodology(chosen);
      notes.push(...method.notes);
    }
    const mixed = method.scoring_versions.length > 1 || method.benchmark_versions.length > 1;

    if (chart === "scatter") {
      const pool = [...chosen, ...peers];
      const points = pool.map(e => ({ name: entryName(e, mixed), submission_id: e.submission_id,
        role: mode === "one_vs_all" && e === chosen[0] ? "focus" : (mode === "one_vs_all" ? "peer" : "entry"),
        scoring_version: e.scoring_version, benchmark_version: e.benchmark_version, x: measure(e, x), y: measure(e, y) }));
      const missing = points.filter(p => p.x === null || p.y === null);
      if (missing.length) {
        const one = missing.length === 1;
        notes.push(`${missing.length} entr${one ? "y has" : "ies have"} no value for ${measureLabel(x, names)} or ${measureLabel(y, names)} and ${one ? "is" : "are"} not plotted.`);
      }
      return { mode, chart, dimension: null, title: `${measureLabel(y, names)} vs ${measureLabel(x, names)}`,
        x_label: measureLabel(x, names), y_label: measureLabel(y, names), x, y, unit: null, dimensions: [], series: [],
        points, notes, comparable: method.comparable };
    }

    const items = {};
    for (const e of [...chosen, ...peers]) items[e.submission_id] = dimItems(e, dimension);
    const keys = new Set();
    for (const e of chosen) Object.keys(items[e.submission_id]).forEach(k => keys.add(k));
    const order = orderKeys(keys, dimension, (opts.order || {})[dimension] || Object.keys(names));
    const val = (id, k) => (items[id][k] === undefined ? null : items[id][k]);
    const series = chosen.map(e => ({ name: entryName(e, mixed), role: mode === "one_vs_all" ? "focus" : "entry",
      submission_id: e.submission_id, scoring_version: e.scoring_version, benchmark_version: e.benchmark_version,
      values: order.map(k => val(e.submission_id, k)) }));
    if (mode === "one_vs_all" && peers.length) {
      const med = [], best = [];
      for (const k of order) {
        const vals = peers.map(p => val(p.submission_id, k)).filter(v => typeof v === "number");
        med.push(vals.length ? median(vals) : null);
        best.push(vals.length ? Math.max(...vals) : null);
      }
      series.push({ name: `All others: median (n=${peers.length})`, role: "median", values: med });
      series.push({ name: `All others: best (n=${peers.length})`, role: "best", values: best });
    }
    if (chart === "radar" && order.length < 3) notes.push("A radar chart needs at least three dimensions.");
    else if (chart === "radar" && order.length > 16) notes.push(`${order.length} dimensions make a crowded radar; the bar or dot chart shows the same data.`);
    return { mode, chart, dimension,
      title: `${DIMENSIONS[dimension]} — ${mode === "one_vs_all" ? "one entry vs all" : "selected entries"}`,
      unit: "%", dimensions: order.map(k => dimLabel(k, dimension, names)), keys: order, series, points: [], notes,
      comparable: method.comparable, x_label: null, y_label: "% of possible points" };
  }

  // ------------------------------------------------------------------ exports
  const csvCell = v => {
    if (v === null || v === undefined) return "";
    const s = String(v);
    return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const csv = rows => rows.map(r => r.map(csvCell).join(",")).join("\n") + "\n";

  function graphCSV(g) {
    if (g.chart === "scatter") {
      return csv([["entry", "submission_id", "scoring_version", "benchmark_version", g.x_label, g.y_label],
        ...g.points.map(p => [p.name, p.submission_id, p.scoring_version, p.benchmark_version, p.x, p.y])]);
    }
    return csv([[DIMENSIONS[g.dimension] || "dimension", ...g.series.map(s => s.name)],
      ...g.dimensions.map((d, i) => [d, ...g.series.map(s => s.values[i])])]);
  }
  const graphJSON = g => JSON.stringify({ format: "rsostb-graph", format_version: "1.0", ...g }, null, 2) + "\n";

  // ------------------------------------------------------------------ layout (same geometry as charts.py)
  const colorOf = (s, i) => NEUTRAL[s.role] || SERIES[i % SERIES.length];
  const fmt = (v, unit) => {
    if (v === null || v === undefined) return "—";
    const s = Math.abs(v) < 1000 ? v.toLocaleString("en-US", { minimumFractionDigits: 1, maximumFractionDigits: 1 })
      : v.toLocaleString("en-US", { maximumFractionDigits: 0 });
    return s + (unit === "%" ? "%" : "");
  };
  const tick = (v, unit) => {
    const s = Number.isInteger(v) || Math.abs(v) >= 100 ? v.toLocaleString("en-US", { maximumFractionDigits: 0 })
      : String(Number(v.toFixed(2)));
    return s.replace("-", "−") + (unit === "%" ? "%" : "");
  };
  function nice(lo, hi, n = 5) {
    if (hi <= lo) hi = lo + 1;
    const raw = (hi - lo) / n, mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => s >= raw);
    const ticks = [];
    let t = Math.floor(lo / step) * step;
    while (t <= hi + step * 0.5 && ticks.length < 12) { ticks.push(Math.round(t * 1e10) / 1e10); t += step; }
    if (ticks[ticks.length - 1] < hi) ticks.push(Math.round((ticks[ticks.length - 1] + step) * 1e10) / 1e10);
    return ticks;
  }
  function domain(values, unit) {
    const vals = values.filter(v => v !== null && v !== undefined);
    const lo = Math.min(0, ...vals);
    let hi = vals.length ? Math.max(0, ...vals) : 1;
    if (unit === "%") hi = hi > 50 ? Math.max(hi, 100) : Math.max(hi, 10);
    return nice(lo, hi);
  }
  const clip = (t, n) => (t.length <= n ? t : t.slice(0, n - 1) + "…");
  function wrap(text, n) {
    const lines = [];
    let cur = "";
    for (const w of text.split(/\s+/).filter(Boolean)) {
      if (cur && cur.length + 1 + w.length > n) { lines.push(cur); cur = w; } else cur = (cur + " " + w).trim();
    }
    return cur ? [...lines, cur] : lines;
  }

  class Canvas {
    constructor(w, h) { this.w = w; this.h = h; this.ops = []; }
    rect(x, y, w, h, fill, title, roundEnd) { this.ops.push(["rect", x, y, Math.max(w, 0), Math.max(h, 0), fill, title, roundEnd]); }
    line(x1, y1, x2, y2, stroke, width = 1, dash = false) { this.ops.push(["line", x1, y1, x2, y2, stroke, width, dash]); }
    circle(cx, cy, r, fill, title, ring = true) { this.ops.push(["circle", cx, cy, r, fill, title, ring]); }
    poly(pts, stroke, width = 2, fill = null, closed = false, dash = false) { this.ops.push(["poly", pts, stroke, width, fill, closed, dash]); }
    text(x, y, s, size = 12, fill = TEXT2, anchor = "start", bold = false, angle = 0) { this.ops.push(["text", x, y, s, size, fill, anchor, bold, angle]); }
  }

  function header(c, g, series) {
    const lined = g.chart === "line" || g.chart === "radar";
    c.text(16, 26, clip(g.title || "", 100), 16, TEXT, "start", true);
    let y = 46;
    for (const n of (g.notes || []).slice(0, 3)) for (const part of wrap(n, 125).slice(0, 2)) { c.text(16, y, part, 11, MUTED); y += 16; }
    if (series.length >= 2) {
      let x = 16;
      series.forEach((s, i) => {
        const col = colorOf(s, i), label = clip(s.name, 60), width = 28 + 6.6 * label.length;
        if (x > 16 && x + width > W - 16) { x = 16; y += 18; }
        if (lined) c.line(x, y + 1, x + 14, y + 1, col, 2, s.role === "best");
        else c.rect(x, y - 4, 14, 10, col);
        c.text(x + 20, y + 5, label, 12, TEXT2);
        x += width;
      });
      y += 18;
    }
    return y + 8;
  }

  function heightFor(g) {
    const nDim = Math.max(1, (g.dimensions || []).length), nSer = Math.max(1, (g.series || []).length);
    const noteLines = (g.notes || []).slice(0, 3).reduce((a, n) => a + Math.min(2, wrap(n, 125).length), 0);
    const extra = 16 * noteLines + (nSer >= 2 ? 18 * Math.ceil(nSer / 2) + 4 : 0);
    if (g.chart === "bar" || g.chart === "dot") {
      const band = g.chart === "bar" ? Math.max(22, nSer * 9 + 10) : 22;
      return Math.floor(70 + extra + nDim * band + 30);
    }
    return Math.floor((g.chart === "radar" ? (nDim > 16 ? 680 : 560) : 440) + extra);
  }

  function layout(g) {
    const series = (g.series || []).slice(0, MAX_SERIES);
    const c = new Canvas(W, heightFor(g));
    c.rect(0, 0, c.w, c.h, SURFACE);
    const top = header(c, g, series);
    if (g.chart === "scatter") scatter(c, g, top);
    else if (!(g.dimensions || []).length || !series.length) c.text(16, top + 20, "Nothing to show yet.", 13, MUTED);
    else if (g.chart === "bar" || g.chart === "dot") horizontal(c, g, series, top, g.chart === "dot");
    else if (g.chart === "column") columns(c, g, series, top);
    else if (g.chart === "line") lines(c, g, series, top);
    else if (g.chart === "radar") radar(c, g, series, top);
    return c;
  }

  const all = series => series.flatMap(s => s.values);

  function horizontal(c, g, series, top, dots) {
    const unit = g.unit, dims = g.dimensions, ticks = domain(all(series), unit);
    const lo = ticks[0], hi = ticks[ticks.length - 1], x0 = 230, x1 = W - 30;
    const band = dots ? 22 : Math.max(22, series.length * 9 + 10), yTop = top + 18, yEnd = yTop + band * dims.length;
    const X = v => x0 + (v - lo) / (hi - lo) * (x1 - x0);
    for (const t of ticks) { c.line(X(t), yTop - 4, X(t), yEnd, GRID, 1); c.text(X(t), yEnd + 16, tick(t, unit), 11, MUTED, "middle"); }
    c.line(X(0), yTop - 4, X(0), yEnd, TEXT2, 1);
    dims.forEach((d, di) => {
      const yb = yTop + di * band;
      c.text(x0 - 8, yb + band / 2 + 4, clip(d, 32), 12, TEXT2, "end");
      if (dots) c.line(x0, yb + band / 2, x1, yb + band / 2, GRID, 1);
      const thick = Math.min(14, (band - 6) / series.length - 2), pad = (band - series.length * (thick + 2)) / 2;
      series.forEach((s, si) => {
        const v = s.values[di];
        if (v === null || v === undefined) return;
        const tip = `${s.name} · ${d}: ${fmt(v, unit)}`;
        if (dots) c.circle(X(v), yb + band / 2, 5, colorOf(s, si), tip);
        else {
          const y = yb + pad + si * (thick + 2), a = Math.min(X(0), X(v)), b = Math.max(X(0), X(v));
          c.rect(a, y, b - a, thick, colorOf(s, si), tip, v >= 0 ? "right" : "left");
        }
      });
    });
  }

  function verticalAxis(c, ticks, unit, x0, x1, y0, y1) {
    const lo = ticks[0], hi = ticks[ticks.length - 1];
    const Y = v => y1 - (v - lo) / (hi - lo) * (y1 - y0);
    for (const t of ticks) { c.line(x0, Y(t), x1, Y(t), GRID, 1); c.text(x0 - 8, Y(t) + 4, tick(t, unit), 11, MUTED, "end"); }
    c.line(x0, Y(0), x1, Y(0), TEXT2, 1);
    return Y;
  }

  function columns(c, g, series, top) {
    const unit = g.unit, dims = g.dimensions, ticks = domain(all(series), unit);
    const x0 = 70, x1 = W - 20, y0 = top + 10, y1 = c.h - 120, Y = verticalAxis(c, ticks, unit, x0, x1, y0, y1);
    const band = (x1 - x0) / dims.length, thick = Math.max(2, Math.min(28, (band - 6) / series.length - 2));
    const pad = (band - series.length * (thick + 2)) / 2;
    dims.forEach((d, di) => {
      const xb = x0 + di * band;
      c.text(xb + band / 2, y1 + 14, clip(d, 22), 11, TEXT2, "end", false, -40);
      series.forEach((s, si) => {
        const v = s.values[di];
        if (v === null || v === undefined) return;
        const a = Math.min(Y(0), Y(v)), b = Math.max(Y(0), Y(v));
        c.rect(xb + pad + si * (thick + 2), a, thick, b - a, colorOf(s, si), `${s.name} · ${d}: ${fmt(v, unit)}`, v >= 0 ? "top" : "bottom");
      });
    });
  }

  function lines(c, g, series, top) {
    const unit = g.unit, dims = g.dimensions, ticks = domain(all(series), unit);
    const x0 = 70, x1 = W - 30, y0 = top + 10, y1 = c.h - 120, Y = verticalAxis(c, ticks, unit, x0, x1, y0, y1);
    const step = dims.length > 1 ? (x1 - x0) / (dims.length - 1) : 0;
    const X = i => (dims.length > 1 ? x0 + i * step : (x0 + x1) / 2);
    dims.forEach((d, di) => c.text(X(di), y1 + 14, clip(d, 22), 11, TEXT2, "end", false, -40));
    series.forEach((s, si) => {
      const col = colorOf(s, si);
      let run = [];
      [...s.values, null].forEach((v, di) => {
        if (v === null || v === undefined) { if (run.length > 1) c.poly(run, col, 2, null, false, s.role === "best"); run = []; return; }
        run.push([X(di), Y(v)]);
      });
      s.values.forEach((v, di) => { if (v !== null && v !== undefined) c.circle(X(di), Y(v), 4, col, `${s.name} · ${dims[di]}: ${fmt(v, unit)}`); });
    });
  }

  function radar(c, g, series, top) {
    const unit = g.unit, dims = g.dimensions;
    if (dims.length < 3) { c.text(16, top + 20, "A radar chart needs at least three dimensions; try bar or line.", 13, MUTED); return; }
    const ticks = domain(all(series), unit), lo = ticks[0], hi = ticks[ticks.length - 1];
    const cx = W / 2, cy = top + (c.h - top) / 2, R = Math.min(W / 2 - 170, (c.h - top) / 2 - (dims.length > 16 ? 90 : 50));
    const P = (i, v) => { const a = -Math.PI / 2 + 2 * Math.PI * i / dims.length, r = (v - lo) / (hi - lo) * R; return [cx + r * Math.cos(a), cy + r * Math.sin(a)]; };
    for (const t of ticks.slice(1)) {
      c.poly(dims.map((_, i) => P(i, t)), GRID, 1, null, true);
      const [x, y] = P(0, t);
      c.text(x + 4, y + 4, tick(t, unit), 10, MUTED);
    }
    const dense = dims.length > 16;
    dims.forEach((d, i) => {
      const [x, y] = P(i, hi);
      c.line(cx, cy, x, y, GRID, 1);
      const [ax, ay] = P(i, hi + (hi - lo) * (0.06 + (dense && i % 2 ? 0.16 : 0)));
      c.text(ax, ay + 4, clip(d, dense ? 22 : 24), dense ? 9 : 11, TEXT2, Math.abs(ax - cx) < 20 ? "middle" : (ax > cx ? "start" : "end"));
    });
    series.forEach((s, si) => {
      const col = colorOf(s, si), vals = s.values, dash = s.role === "best";
      const known = v => v !== null && v !== undefined;
      if (vals.every(known)) {
        c.poly(vals.map((v, i) => P(i, v)), col, 2, s.role === "median" || s.role === "best" ? null : col, true, dash);
      } else {
        for (let i = 0; i < vals.length; i++) {
          const j = (i + 1) % vals.length;
          if (known(vals[i]) && known(vals[j])) c.poly([P(i, vals[i]), P(j, vals[j])], col, 2, null, false, dash);
        }
      }
      s.values.forEach((v, i) => { if (v !== null && v !== undefined) { const [px, py] = P(i, v); c.circle(px, py, 3.5, col, `${s.name} · ${dims[i]}: ${fmt(v, unit)}`); } });
    });
  }

  function scatter(c, g, top) {
    const pts = (g.points || []).filter(p => p.x !== null && p.y !== null);
    if (!pts.length) { c.text(16, top + 20, "No entry has both values.", 13, MUTED); return; }
    const xt = nice(Math.min(0, ...pts.map(p => p.x)), Math.max(0, ...pts.map(p => p.x)));
    const yt = nice(Math.min(0, ...pts.map(p => p.y)), Math.max(0, ...pts.map(p => p.y)));
    const x0 = 90, x1 = W - 30, y0 = top + 30, y1 = c.h - 60, Y = verticalAxis(c, yt, null, x0, x1, y0, y1);
    const X = v => x0 + (v - xt[0]) / (xt[xt.length - 1] - xt[0]) * (x1 - x0);
    for (const t of xt) { c.line(X(t), y0, X(t), y1, GRID, 1); c.text(X(t), y1 + 16, tick(t), 11, MUTED, "middle"); }
    c.text((x0 + x1) / 2, c.h - 18, g.x_label || "", 12, TEXT2, "middle");
    c.text(16, y0 - 16, `↑ ${g.y_label || ""}`, 12, TEXT2);
    const colored = pts.filter(p => p.role !== "peer");
    for (const p of [...pts].sort((a, b) => (a.role !== "peer") - (b.role !== "peer"))) {
      const idx = colored.indexOf(p);
      const col = p.role === "peer" ? NEUTRAL.peer : (idx < 3 ? SERIES[idx] : NEUTRAL.median);
      c.circle(X(p.x), Y(p.y), 5, col, `${p.name}: ${fmt(p.x)}, ${fmt(p.y)}`);
    }
    for (const p of colored.slice(0, 4)) c.text(X(p.x) + 8, Y(p.y) - 8, clip(p.name, 30), 11, TEXT);
  }

  // ------------------------------------------------------------------ SVG
  const esc = s => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#x27;");
  const n = v => String(Math.round(v * 10) / 10);
  function roundRectPath(x, y, w, h, end, r = 4) {
    r = Math.min(r, w / 2, h / 2);
    if (end === "right") return `M${x},${y}h${w - r}a${r},${r} 0 0 1 ${r},${r}v${h - 2 * r}a${r},${r} 0 0 1 ${-r},${r}h${-(w - r)}z`;
    if (end === "left") return `M${x + w},${y}v${h}h${-(w - r)}a${r},${r} 0 0 1 ${-r},${-r}v${-(h - 2 * r)}a${r},${r} 0 0 1 ${r},${-r}z`;
    if (end === "top") return `M${x},${y + h}v${-(h - r)}a${r},${r} 0 0 1 ${r},${-r}h${w - 2 * r}a${r},${r} 0 0 1 ${r},${r}v${h - r}z`;
    return `M${x},${y}h${w}v${h - r}a${r},${r} 0 0 1 ${-r},${r}h${-(w - 2 * r)}a${r},${r} 0 0 1 ${-r},${-r}z`;
  }
  function toSVG(g) {
    const c = layout(g);
    const out = [`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${c.w} ${c.h}" width="${c.w}" height="${c.h}" role="img" aria-label="${esc(g.title || "chart")}" font-family="${esc(FONT)}">`];
    for (const op of c.ops) {
      const k = op[0];
      if (k === "rect") {
        const [, x, y, w, h, fill, tip, end] = op, rounded = end && w > 1 && h > 1;
        const shape = rounded ? `<path d="${roundRectPath(x, y, w, h, end)}" fill="${fill}"` : `<rect x="${n(x)}" y="${n(y)}" width="${n(w)}" height="${n(h)}" fill="${fill}"`;
        out.push(shape + (tip ? `><title>${esc(tip)}</title></${rounded ? "path" : "rect"}>` : "/>"));
      } else if (k === "line") {
        const [, x1, y1, x2, y2, stroke, width, dash] = op;
        out.push(`<line x1="${n(x1)}" y1="${n(y1)}" x2="${n(x2)}" y2="${n(y2)}" stroke="${stroke}" stroke-width="${width}"${dash ? ' stroke-dasharray="5 4"' : ""}/>`);
      } else if (k === "circle") {
        const [, cx, cy, r, fill, tip, ring] = op;
        out.push(`<circle cx="${n(cx)}" cy="${n(cy)}" r="${r}" fill="${fill}"${ring ? ` stroke="${SURFACE}" stroke-width="2"` : ""}` + (tip ? `><title>${esc(tip)}</title></circle>` : "/>"));
      } else if (k === "poly") {
        const [, pts, stroke, width, fill, closed, dash] = op, tag = closed ? "polygon" : "polyline";
        out.push(`<${tag} points="${pts.map(([x, y]) => `${n(x)},${n(y)}`).join(" ")}" stroke="${stroke}" stroke-width="${width}" stroke-linejoin="round" fill="${fill || "none"}"${fill ? ' fill-opacity="0.12"' : ""}${dash ? ' stroke-dasharray="5 4"' : ""}/>`);
      } else if (k === "text") {
        const [, x, y, s, size, fill, anchor, bold, angle] = op;
        out.push(`<text x="${n(x)}" y="${n(y)}" font-size="${size}" fill="${fill}" text-anchor="${anchor}"${bold ? ' font-weight="600"' : ""}${angle ? ` transform="rotate(${angle} ${n(x)} ${n(y)})"` : ""}>${esc(s)}</text>`);
      }
    }
    out.push("</svg>");
    return out.join("\n");
  }

  const api = { CHART_TYPES, MODES, DIMENSIONS, SCATTER_MEASURES, LEVEL_NAMES, DIFFICULTY_ORDER, SERIES, MAX_SERIES,
    scoringVersions, benchmarkVersions, methodology, entryName, graph, graphCSV, graphJSON, toSVG, measure, cmpKey };
  root.RSOSTBCharts = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
