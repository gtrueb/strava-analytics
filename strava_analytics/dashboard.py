"""Renders a self-contained dashboard.html from the analysis payload.

No CDN, no build step: the JSON payload is embedded and the charts are hand-drawn
SVG, so the file works offline and can be moved anywhere.
"""

from __future__ import annotations

import json
import math
import webbrowser

from .analysis import build_payload
from .config import DASHBOARD_PATH

TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Training dashboard</title>
<style>
:root {
  color-scheme: light;
  --surface-1: #fcfcfb;
  --plane: #f9f9f7;
  --text-primary: #0b0b0b;
  --text-secondary: #52514e;
  --muted: #898781;
  --grid: #e1e0d9;
  --axis: #c3c2b7;
  --border: rgba(11,11,11,0.10);
  --good: #006300;
  --series-1: #2a78d6;
  --series-2: #eb6834;
  --series-3: #1baf7a;
  --series-4: #eda100;
  --ord-1: #86b6ef;
  --ord-2: #2a78d6;
  --ord-3: #0d366b;
  --pos: #2a78d6;
  --neg: #e34948;
  --neutral: #f0efec;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --surface-1: #1a1a19;
    --plane: #0d0d0d;
    --text-primary: #ffffff;
    --text-secondary: #c3c2b7;
    --muted: #898781;
    --grid: #2c2c2a;
    --axis: #383835;
    --border: rgba(255,255,255,0.10);
    --good: #0ca30c;
    --series-1: #3987e5;
    --series-2: #d95926;
    --series-3: #199e70;
    --series-4: #c98500;
    --ord-1: #b7d3f6;
    --ord-2: #3987e5;
    --ord-3: #184f95;
    --pos: #3987e5;
    --neg: #e66767;
    --neutral: #383835;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --surface-1: #1a1a19;
  --plane: #0d0d0d;
  --text-primary: #ffffff;
  --text-secondary: #c3c2b7;
  --muted: #898781;
  --grid: #2c2c2a;
  --axis: #383835;
  --border: rgba(255,255,255,0.10);
  --good: #0ca30c;
  --series-1: #3987e5;
  --series-2: #d95926;
  --series-3: #199e70;
  --series-4: #c98500;
  --ord-1: #b7d3f6;
  --ord-2: #3987e5;
  --ord-3: #184f95;
  --pos: #3987e5;
  --neg: #e66767;
  --neutral: #383835;
}

* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--plane);
  color: var(--text-primary);
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  font-size: 15px;
  line-height: 1.5;
  -webkit-font-smoothing: antialiased;
}
.wrap { max-width: 1180px; margin: 0 auto; padding: 32px 20px 80px; }

header.top { display: flex; flex-wrap: wrap; gap: 16px; align-items: baseline; justify-content: space-between; margin-bottom: 8px; }
h1 { font-size: 26px; font-weight: 650; margin: 0; letter-spacing: -0.01em; }
.sub { color: var(--text-secondary); font-size: 14px; margin: 2px 0 0; }

.controls { display: flex; gap: 8px; align-items: center; }
button.ctl {
  font: inherit; font-size: 13px; padding: 6px 12px; border-radius: 999px;
  border: 1px solid var(--border); background: var(--surface-1); color: var(--text-secondary); cursor: pointer;
}
button.ctl:hover { color: var(--text-primary); }
button.ctl[aria-pressed="true"] { color: var(--text-primary); border-color: var(--axis); }

.hero { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 12px; margin: 24px 0 28px; }
.tile { background: var(--surface-1); border: 1px solid var(--border); border-radius: 14px; padding: 16px 18px; }
.tile .label { font-size: 12px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); }
.tile .value { font-size: 30px; font-weight: 650; margin-top: 6px; letter-spacing: -0.02em; }
.tile .value .unit { font-size: 15px; font-weight: 500; color: var(--text-secondary); margin-left: 3px; }
.tile .note { font-size: 13px; color: var(--text-secondary); margin-top: 4px; }
.delta-up { color: var(--good); }
.delta-down { color: var(--neg); }

.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(440px, 1fr)); gap: 16px; }
.card { background: var(--surface-1); border: 1px solid var(--border); border-radius: 14px; padding: 18px 18px 12px; min-width: 0; }
.card.wide { grid-column: 1 / -1; }
.card h2 { font-size: 15px; font-weight: 620; margin: 0; }
.card p.hint { font-size: 13px; color: var(--text-secondary); margin: 4px 0 12px; }
.legend { display: flex; flex-wrap: wrap; gap: 14px; margin: 0 0 6px; padding: 0; list-style: none; font-size: 13px; color: var(--text-secondary); }
.legend li { display: flex; align-items: center; gap: 6px; }
.swatch { width: 12px; height: 12px; border-radius: 3px; flex: none; }
.swatch.line { height: 3px; border-radius: 2px; width: 16px; }
.plot { width: 100%; overflow: visible; display: block; }
.plot text { font-size: 11px; fill: var(--muted); font-variant-numeric: tabular-nums; }
.plot .lbl { fill: var(--text-secondary); font-size: 11.5px; font-weight: 600; }
.plot .grid line { stroke: var(--grid); stroke-width: 1; }
.plot .axis line { stroke: var(--axis); stroke-width: 1; }

.tooltip {
  position: fixed; pointer-events: none; z-index: 40; opacity: 0; transition: opacity .1s;
  background: var(--surface-1); color: var(--text-primary);
  border: 1px solid var(--border); border-radius: 10px; padding: 8px 10px;
  font-size: 12.5px; box-shadow: 0 8px 24px rgba(0,0,0,.16); min-width: 130px;
}
.tooltip .tt-title { font-weight: 620; margin-bottom: 4px; }
.tooltip .tt-row { display: flex; align-items: center; gap: 6px; justify-content: space-between; }
.tooltip .tt-row .k { display: flex; align-items: center; gap: 6px; color: var(--text-secondary); }
.tooltip .tt-row .v { font-variant-numeric: tabular-nums; font-weight: 600; }

table { width: 100%; border-collapse: collapse; font-size: 13.5px; font-variant-numeric: tabular-nums; }
th, td { text-align: right; padding: 7px 8px; border-bottom: 1px solid var(--border); white-space: nowrap; }
th:first-child, td:first-child { text-align: left; }
thead th { color: var(--muted); font-weight: 600; font-size: 12px; text-transform: uppercase; letter-spacing: 0.04em; }
tbody tr:last-child td { border-bottom: none; }
.scroll { overflow-x: auto; }
.tableview { margin-top: 10px; border-top: 1px solid var(--border); padding-top: 8px; }
.tableview[hidden] { display: none; }
.section-title { font-size: 15px; font-weight: 620; margin: 30px 0 10px; }
.pill { display: inline-block; font-size: 11.5px; padding: 1px 8px; border-radius: 999px; border: 1px solid var(--border); color: var(--text-secondary); }
footer.notes { margin-top: 36px; font-size: 13px; color: var(--text-secondary); }
footer.notes h3 { font-size: 13px; color: var(--text-primary); margin: 0 0 6px; }
footer.notes li { margin-bottom: 4px; }
.empty { color: var(--muted); font-size: 13.5px; padding: 24px 0; text-align: center; }
.range { display: flex; gap: 4px; margin: 0 0 8px; }
.range button {
  font: inherit; font-size: 12px; padding: 3px 10px; border-radius: 7px; cursor: pointer;
  border: 1px solid var(--border); background: transparent; color: var(--muted);
}
.range button:hover { color: var(--text-primary); }
.range button[aria-pressed="true"] {
  color: var(--text-primary); border-color: var(--axis); background: var(--surface-1); font-weight: 600;
}
.range button:focus-visible { outline: 2px solid var(--series-1); outline-offset: 1px; }
.multiples { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 16px; }
.multiple h3 { font-size: 12.5px; font-weight: 620; margin: 0 0 2px; color: var(--text-secondary); display: flex; align-items: center; gap: 6px; }
@media (max-width: 560px) {
  .cards { grid-template-columns: 1fr; }
  .card { padding: 14px 12px 10px; }
  h1 { font-size: 21px; }
}
</style>
</head>
<body>
<div class="wrap">
  <header class="top">
    <div>
      <h1 id="title">Training dashboard</h1>
      <p class="sub" id="subtitle"></p>
    </div>
    <div class="controls">
      <button class="ctl" id="tables-toggle" aria-pressed="false">Show data tables</button>
      <button class="ctl" id="theme-toggle">Theme</button>
    </div>
  </header>

  <section class="hero" id="hero"></section>
  <section class="cards" id="cards"></section>
  <div id="tables"></div>

  <footer class="notes">
    <h3>How these numbers are made</h3>
    <ul id="methodology"></ul>
  </footer>
</div>

<div class="tooltip" id="tooltip" role="status" aria-live="polite"></div>

<script id="payload" type="application/json">__DATA__</script>
<script>
const DATA = JSON.parse(document.getElementById('payload').textContent);
const SERIES_VARS = ['--series-1','--series-2','--series-3','--series-4'];
const tooltip = document.getElementById('tooltip');

/* ------------------------------------------------------------------ format */
const fmtDur = s => {
  if (s == null || isNaN(s)) return '–';
  s = Math.round(s);
  const h = Math.floor(s/3600), m = Math.floor((s%3600)/60), x = s%60;
  return h ? `${h}:${String(m).padStart(2,'0')}:${String(x).padStart(2,'0')}`
           : `${m}:${String(x).padStart(2,'0')}`;
};
const fmtPace = s => {
  if (!s || isNaN(s)) return '–';
  const m = Math.floor(s/60), x = Math.round(s%60);
  return `${m}:${String(x).padStart(2,'0')}`;
};
const fmtNum = (v, d=0) => v == null || isNaN(v) ? '–' : v.toLocaleString('en-GB',{minimumFractionDigits:d, maximumFractionDigits:d});
const cssVar = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const el = (tag, cls, html) => { const n = document.createElement(tag); if (cls) n.className = cls; if (html != null) n.innerHTML = html; return n; };
const svgEl = (tag, attrs={}) => { const n = document.createElementNS('http://www.w3.org/2000/svg', tag); for (const k in attrs) n.setAttribute(k, attrs[k]); return n; };

function niceTicks(min, max, count) {
  if (min === max) { min -= 1; max += 1; }
  const span = max - min;
  const raw = span / Math.max(1, count);
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const norm = raw / mag;
  const step = (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 2.5 ? 2.5 : norm <= 5 ? 5 : 10) * mag;
  const out = [];
  for (let v = Math.ceil(min/step)*step; v <= max + step*1e-9; v += step) out.push(+v.toFixed(10));
  return out;
}

/* ------------------------------------------------------------------ charts */
/* Shared line/area chart. Series: {id,label,color,emphasis,points:[{x,y,meta}]} */
function lineChart(host, cfg) {
  const render = () => {
    host.innerHTML = '';
    const W = Math.max(320, host.clientWidth || 640);
    const H = cfg.height || 260;
    const pad = { t: 14, r: cfg.padRight != null ? cfg.padRight : 58, b: 30, l: 46 };
    const iw = W - pad.l - pad.r, ih = H - pad.t - pad.b;

    const all = cfg.series.flatMap(s => s.points).filter(p => p.y != null);
    if (!all.length) { host.appendChild(el('div','empty','No data yet')); return; }
    const xs = all.map(p => p.x), ys = all.map(p => p.y);
    let x0 = cfg.xMin != null ? cfg.xMin : Math.min(...xs);
    let x1 = cfg.xMax != null ? cfg.xMax : Math.max(...xs);
    let y0 = cfg.yMin != null ? cfg.yMin : Math.min(0, Math.min(...ys));
    let y1 = cfg.yMax != null ? cfg.yMax : Math.max(...ys);
    if (cfg.yPad !== false && cfg.yMax == null) y1 += (y1 - y0) * 0.08;
    if (x0 === x1) { x0 -= 0.5; x1 += 0.5; }

    const sx = v => pad.l + (x1 === x0 ? iw/2 : (v - x0) / (x1 - x0) * iw);
    const sy = v => pad.t + ih - (y1 === y0 ? ih/2 : (v - y0) / (y1 - y0) * ih);

    const svg = svgEl('svg', { class: 'plot', viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: 'img' });
    svg.setAttribute('aria-label', cfg.ariaLabel || 'chart');

    const gGrid = svgEl('g', { class: 'grid' });
    const yTicks = niceTicks(y0, y1, cfg.yTickCount || 4);
    yTicks.forEach(t => {
      gGrid.appendChild(svgEl('line', { x1: pad.l, x2: pad.l + iw, y1: sy(t), y2: sy(t) }));
      const label = svgEl('text', { x: pad.l - 8, y: sy(t) + 4, 'text-anchor': 'end' });
      label.textContent = (cfg.formatY || (v => fmtNum(v)))(t);
      gGrid.appendChild(label);
    });
    svg.appendChild(gGrid);

    const gAxis = svgEl('g', { class: 'axis' });
    gAxis.appendChild(svgEl('line', { x1: pad.l, x2: pad.l + iw, y1: pad.t + ih, y2: pad.t + ih }));
    const seenTicks = new Set();
    (cfg.xTicks || niceTicks(x0, x1, 6)).forEach(t => {
      if (t < x0 || t > x1) return;
      const text = (cfg.formatXTick || (v => fmtNum(v)))(t);
      // A narrow domain can format several ticks identically ("Oct 25" four times).
      if (seenTicks.has(text)) return;
      seenTicks.add(text);
      const label = svgEl('text', { x: sx(t), y: pad.t + ih + 18, 'text-anchor': 'middle' });
      label.textContent = text;
      gAxis.appendChild(label);
    });
    svg.appendChild(gAxis);

    (cfg.marks || []).forEach(m => {
      if (m.x < x0 || m.x > x1) return;
      svg.appendChild(svgEl('line', { x1: sx(m.x), x2: sx(m.x), y1: pad.t, y2: pad.t + ih,
        stroke: cssVar('--axis'), 'stroke-width': 1, 'stroke-dasharray': '3 3' }));
      const t = svgEl('text', { x: sx(m.x) + 4, y: pad.t + 10 });
      t.setAttribute('fill', cssVar('--muted'));
      t.textContent = m.label;
      svg.appendChild(t);
    });

    if (cfg.zeroLine) {
      svg.appendChild(svgEl('line', { x1: pad.l, x2: pad.l + iw, y1: sy(0), y2: sy(0), stroke: cssVar('--axis'), 'stroke-width': 1 }));
    }

    const endLabels = [];
    cfg.series.forEach(s => {
      const pts = s.points.filter(p => p.y != null).sort((a,b) => a.x - b.x);
      if (!pts.length) return;
      const d = pts.map((p,i) => `${i ? 'L' : 'M'}${sx(p.x).toFixed(1)},${sy(p.y).toFixed(1)}`).join(' ');
      if (s.area) {
        const area = `${d} L${sx(pts[pts.length-1].x).toFixed(1)},${sy(0).toFixed(1)} L${sx(pts[0].x).toFixed(1)},${sy(0).toFixed(1)} Z`;
        svg.appendChild(svgEl('path', { d: area, fill: s.color, 'fill-opacity': s.areaOpacity || 0.12, stroke: 'none' }));
      }
      if (!s.dotsOnly) {
        svg.appendChild(svgEl('path', {
          d, fill: 'none', stroke: s.color,
          'stroke-width': s.emphasis ? 3 : 2,
          'stroke-linecap': 'round', 'stroke-linejoin': 'round',
          'stroke-dasharray': s.dashed ? '5 4' : 'none'
        }));
      }
      if (s.dots || s.dotsOnly) {
        pts.forEach(p => {
          svg.appendChild(svgEl('circle', { cx: sx(p.x), cy: sy(p.y), r: s.dotsOnly ? 4 : 3.2,
            fill: s.color, stroke: cssVar('--surface-1'), 'stroke-width': 2 }));
        });
      }
      // Direct end label: identity never rests on colour alone.
      if (cfg.directLabels !== false) {
        const last = pts[pts.length-1];
        svg.appendChild(svgEl('circle', { cx: sx(last.x), cy: sy(last.y), r: s.emphasis ? 5 : 4,
          fill: s.color, stroke: cssVar('--surface-1'), 'stroke-width': 2 }));
        endLabels.push({ x: sx(last.x) + 9, y: sy(last.y), text: s.label });
      }
    });

    // Nudge end labels apart so overlapping series stay legible.
    if (endLabels.length) {
      endLabels.sort((a,b) => a.y - b.y);
      const minGap = 13;
      for (let i = 1; i < endLabels.length; i++) {
        if (endLabels[i].y - endLabels[i-1].y < minGap) endLabels[i].y = endLabels[i-1].y + minGap;
      }
      const overflow = endLabels[endLabels.length-1].y - (pad.t + ih);
      if (overflow > 0) endLabels.forEach(l => l.y -= overflow);
      endLabels.forEach(l => {
        const t = svgEl('text', { x: l.x, y: l.y + 4, class: 'lbl' });
        t.setAttribute('fill', cssVar('--text-secondary'));
        t.textContent = l.text;
        svg.appendChild(t);
      });
    }

    // Crosshair + tooltip
    const crosshair = svgEl('line', { y1: pad.t, y2: pad.t + ih, stroke: cssVar('--axis'), 'stroke-width': 1, opacity: 0 });
    svg.appendChild(crosshair);
    const markers = svgEl('g', { opacity: 0 });
    svg.appendChild(markers);

    const xKeys = [...new Set(all.map(p => p.x))].sort((a,b) => a-b);
    const overlay = svgEl('rect', { x: pad.l, y: pad.t, width: iw, height: ih, fill: 'transparent' });
    svg.appendChild(overlay);

    const move = ev => {
      const rect = svg.getBoundingClientRect();
      const px = (ev.clientX - rect.left) * (W / rect.width);
      let nearest = xKeys[0], best = Infinity;
      xKeys.forEach(k => { const d = Math.abs(sx(k) - px); if (d < best) { best = d; nearest = k; } });
      crosshair.setAttribute('x1', sx(nearest)); crosshair.setAttribute('x2', sx(nearest));
      crosshair.setAttribute('opacity', 1);
      markers.innerHTML = ''; markers.setAttribute('opacity', 1);
      const rows = [];
      cfg.series.forEach(s => {
        const p = s.points.find(p => p.x === nearest && p.y != null);
        if (!p) return;
        markers.appendChild(svgEl('circle', { cx: sx(p.x), cy: sy(p.y), r: 5, fill: s.color, stroke: cssVar('--surface-1'), 'stroke-width': 2 }));
        rows.push(`<div class="tt-row"><span class="k"><span class="swatch" style="background:${s.color}"></span>${s.label}</span><span class="v">${(cfg.formatTooltip || cfg.formatY || (v=>fmtNum(v)))(p.y, p)}</span></div>`);
      });
      if (!rows.length) { hide(); return; }
      tooltip.innerHTML = `<div class="tt-title">${(cfg.formatXTooltip || cfg.formatXTick || (v=>fmtNum(v)))(nearest)}</div>${rows.join('')}`;
      tooltip.style.opacity = 1;
      const tr = tooltip.getBoundingClientRect();
      let left = ev.clientX + 14;
      if (left + tr.width > window.innerWidth - 8) left = ev.clientX - tr.width - 14;
      tooltip.style.left = Math.max(8, left) + 'px';
      tooltip.style.top = Math.min(window.innerHeight - tr.height - 8, Math.max(8, ev.clientY - tr.height/2)) + 'px';
    };
    const hide = () => { tooltip.style.opacity = 0; crosshair.setAttribute('opacity', 0); markers.setAttribute('opacity', 0); };
    svg.addEventListener('mousemove', move);
    svg.addEventListener('mouseleave', hide);
    host.appendChild(svg);
  };
  render();
  new ResizeObserver(() => render()).observe(host);
}

/* Stacked horizontal bars, 2px surface gaps between segments. */
function stackedBars(host, cfg) {
  const render = () => {
    host.innerHTML = '';
    const W = Math.max(320, host.clientWidth || 640);
    const rowH = 34, gap = 12, padL = 62, padR = 8, padT = 4, padB = 22;
    const H = padT + padB + cfg.rows.length * (rowH + gap);
    const iw = W - padL - padR;
    const svg = svgEl('svg', { class: 'plot', viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: 'img' });
    svg.setAttribute('aria-label', cfg.ariaLabel || 'stacked bars');

    cfg.rows.forEach((row, i) => {
      const y = padT + i * (rowH + gap);
      const label = svgEl('text', { x: padL - 10, y: y + rowH/2 + 4, 'text-anchor': 'end', class: 'lbl' });
      label.setAttribute('fill', cssVar('--text-secondary'));
      label.textContent = row.label;
      svg.appendChild(label);

      const total = row.segments.reduce((a,s) => a + s.value, 0) || 1;
      let x = padL;
      row.segments.forEach(seg => {
        const w = seg.value / total * iw;
        if (w <= 0) return;
        const rect = svgEl('rect', { x: x + 1, y, width: Math.max(0, w - 2), height: rowH, rx: 4, fill: seg.color });
        rect.addEventListener('mousemove', ev => {
          tooltip.innerHTML = `<div class="tt-title">${row.label}</div>` +
            row.segments.map(s => `<div class="tt-row"><span class="k"><span class="swatch" style="background:${s.color}"></span>${s.label}</span><span class="v">${fmtNum(s.value,1)} km · ${fmtNum(100*s.value/total,0)}%</span></div>`).join('');
          tooltip.style.opacity = 1;
          const tr = tooltip.getBoundingClientRect();
          let left = ev.clientX + 14;
          if (left + tr.width > window.innerWidth - 8) left = ev.clientX - tr.width - 14;
          tooltip.style.left = Math.max(8,left) + 'px';
          tooltip.style.top = Math.max(8, ev.clientY - tr.height/2) + 'px';
        });
        rect.addEventListener('mouseleave', () => tooltip.style.opacity = 0);
        svg.appendChild(rect);
        const pct = 100 * seg.value / total;
        // Only label inside the segment when the text actually fits.
        if (w > 46 && pct >= 8) {
          const t = svgEl('text', { x: x + w/2, y: y + rowH/2 + 4, 'text-anchor': 'middle' });
          t.setAttribute('fill', seg.ink || '#fcfcfb');
          t.setAttribute('font-weight', '600');
          t.textContent = `${Math.round(pct)}%`;
          svg.appendChild(t);
        }
        x += w;
      });
    });
    host.appendChild(svg);
  };
  render();
  new ResizeObserver(() => render()).observe(host);
}

/* --------------------------------------------------------------- structure */
function card(title, hint, opts={}) {
  const c = el('div', 'card' + (opts.wide ? ' wide' : ''));
  c.appendChild(el('h2', null, title));
  if (hint) c.appendChild(el('p', 'hint', hint));
  if (opts.legend) {
    const ul = el('ul', 'legend');
    opts.legend.forEach(item => {
      const li = el('li');
      li.innerHTML = `<span class="swatch ${item.line ? 'line' : ''}" style="background:${item.color}"></span>${item.label}`;
      ul.appendChild(li);
    });
    c.appendChild(ul);
  }
  const plot = el('div');
  c.appendChild(plot);
  if (opts.table) {
    const tv = el('div', 'tableview scroll');
    tv.hidden = true;
    tv.appendChild(opts.table);
    c.appendChild(tv);
  }
  return { card: c, plot };
}

function makeTable(headers, rows) {
  const t = el('table');
  const thead = el('thead');
  const tr = el('tr');
  headers.forEach(h => { const th = el('th', null, h); tr.appendChild(th); });
  thead.appendChild(tr); t.appendChild(thead);
  const tbody = el('tbody');
  rows.forEach(r => {
    const trr = el('tr');
    r.forEach(cell => { const td = el('td', null, cell == null ? '–' : String(cell)); trr.appendChild(td); });
    tbody.appendChild(trr);
  });
  t.appendChild(tbody);
  return t;
}

/* ------------------------------------------------------------------ builds */
const buildsAll = (() => {
  const list = DATA.builds.slice();
  if (DATA.current) list.push(DATA.current);
  return list.filter(b => b.weeks && b.weeks.length);
})();

const buildYear = b => (b.race && b.race.date) ? b.race.date.slice(0,4) : '?';
// Colour follows the entity (the year), assigned once in chronological order.
const yearOrder = [...new Set(buildsAll.map(buildYear))].sort();
const colorFor = b => cssVar(SERIES_VARS[yearOrder.indexOf(buildYear(b)) % SERIES_VARS.length]);

/* Completed weeks only — the in-progress week would read as a collapse in volume. */
function visibleWeeks(b) {
  if (!b.is_current) return b.weeks;
  const cutoff = b.completed_cutoff != null ? b.completed_cutoff : (b.weeks_to_go || 0);
  return b.weeks.filter(w => w.weeks_out >= cutoff);
}
/* Everything logged so far, including the part-week — right for running totals. */
function weeksToDate(b) {
  if (!b.is_current) return b.weeks;
  return b.weeks.filter(w => w.weeks_out >= (b.weeks_to_go || 0));
}

function buildSeries(valueFn, opts={}) {
  return buildsAll.map(b => ({
    id: buildYear(b),
    label: buildYear(b) + (b.is_current ? ' (now)' : ''),
    color: colorFor(b),
    emphasis: !!b.is_current,
    dots: opts.dots,
    area: opts.area,
    points: visibleWeeks(b).map(w => ({ x: -w.weeks_out, y: valueFn(w, b), meta: w }))
  })).filter(s => s.points.some(p => p.y != null));
}

const xTickFmt = v => v === 0 ? 'race' : `${-v}w`;
const xTicksBuild = (() => {
  const n = DATA.build_weeks;
  const out = [];
  for (let w = n - 1; w >= 0; w -= Math.max(1, Math.round(n/8))) out.push(-w);
  if (!out.includes(0)) out.push(0);
  return out;
})();

/* --------------------------------------------------------------------- UI */
document.getElementById('subtitle').textContent =
  `${DATA.totals.runs} runs · ${fmtNum(DATA.totals.km)} km · ${DATA.totals.first} → ${DATA.totals.last} · generated ${DATA.generated_at.replace('T',' ')}`;

/* Hero tiles */
const hero = document.getElementById('hero');
function tile(label, value, unit, note, noteCls) {
  const t = el('div','tile');
  t.appendChild(el('div','label',label));
  t.appendChild(el('div','value', value + (unit ? `<span class="unit">${unit}</span>` : '')));
  if (note) t.appendChild(el('div','note' + (noteCls ? ' ' + noteCls : ''), note));
  hero.appendChild(t);
}

const cmp = DATA.comparison;
if (DATA.current && DATA.current.race) {
  const r = DATA.current.race;
  const fmtDate = d => new Date(d).toLocaleDateString('en-GB',{day:'numeric', month:'short'});
  tile('Target race', (r.projected ? '≈ ' : '') + fmtDate(r.date),
       '', `${DATA.current.weeks_to_go} weeks to go${r.projected ? ' · date projected from past years' : ''}`);
}
if (cmp) {
  const d = cmp.delta_pct_vs_prev_avg;
  tile(`Last ${cmp.window_weeks} weeks`, fmtNum(cmp.current_km), 'km',
       d == null ? 'no comparable history' :
       `${d >= 0 ? '+' : ''}${fmtNum(d,0)}% vs your average build at this point`,
       d == null ? null : (d >= 0 ? 'delta-up' : 'delta-down'));
  if (cmp.cum_current != null) {
    const dc = cmp.cum_delta_pct;
    tile('Block so far', fmtNum(cmp.cum_current), 'km',
         (dc == null ? 'total since the block started, this week included' :
          `${dc >= 0 ? '+' : ''}${fmtNum(dc,0)}% vs previous builds, this week included`),
         dc == null ? null : (dc >= 0 ? 'delta-up' : 'delta-down'));
  }
}
if (DATA.fitness && DATA.fitness.length) {
  const last = DATA.fitness[DATA.fitness.length - 1];
  tile('Fitness (CTL)', fmtNum(last.ctl), '',
       `form ${last.tsb >= 0 ? '+' : ''}${fmtNum(last.tsb)} · fatigue ${fmtNum(last.atl)}`);
}
const proj = DATA.projection;
if (proj) {
  const gain = proj.pb_delta_s;
  tile('If you finish the plan', fmtDur(proj.scenarios.plan_completed), '',
       gain == null ? `from your ${proj.baseline.name} ${fmtDur(proj.baseline.time_s)}`
                    : `${gain > 0 ? fmtDur(gain) + ' under' : fmtDur(-gain) + ' over'} your ${fmtDur(proj.best_previous_s)} PB`,
       gain == null ? null : (gain > 0 ? 'delta-up' : 'delta-down'));
} else if (DATA.prediction) {
  const p = DATA.prediction;
  const best = (DATA.races || []).filter(r => r.time_s).sort((a,b) => a.time_s - b.time_s)[0];
  tile('Projected half', fmtDur(p.predicted_s), '',
       `from your ${p.from} (${fmtDur(p.from_time_s)}, ${p.from_date})` + (best ? ` · PB ${fmtDur(best.time_s)}` : ''));
}

/* Cards */
const cards = document.getElementById('cards');

if (buildsAll.length) {
  const legend = buildsAll.map(b => ({ label: buildYear(b) + (b.is_current ? ' (now)' : ''), color: colorFor(b), line: true }));

  // 1. Weekly volume
  {
    const rows = [];
    for (let w = DATA.build_weeks - 1; w >= 0; w--) {
      rows.push([xTickFmt(-w), ...buildsAll.map(b => {
        const hit = visibleWeeks(b).find(x => x.weeks_out === w);
        return hit ? fmtNum(hit.km,1) : null;
      })]);
    }
    // Remaining prescribed sessions, drawn dashed so planned is never mistaken for done.
    const planned = (DATA.projection && DATA.projection.planned_weeks) || [];
    const plannedLegend = legend.slice();
    const volumeSeries = buildSeries(w => w.km, { dots: true });
    if (planned.length) {
      const currentBuild = buildsAll.find(b => b.is_current);
      const pts = planned.map(w => ({ x: -w.weeks_out, y: w.km }));
      volumeSeries.push({
        id: 'planned', label: 'planned', color: cssVar('--series-4'),
        dashed: true, dots: true, points: pts.sort((a, b) => a.x - b.x)
      });
      plannedLegend.push({ label: 'planned (Runna)', color: cssVar('--series-4'), line: true });
    }
    const { card: c, plot } = card('Weekly volume through the build',
      `Kilometres per week, every build aligned on race day. ${DATA.build_weeks}-week window`
      + (DATA.current && DATA.current.week_in_progress
         ? `. This week is included and still running \u2014 ${DATA.current.week_days_done} of 7 days done, so its bar will keep growing.` : '.'),
      { legend: plannedLegend, wide: true, table: makeTable(['Week', ...buildsAll.map(b => buildYear(b))], rows) });
    cards.appendChild(c);
    lineChart(plot, {
      series: volumeSeries,
      xTicks: xTicksBuild, formatXTick: xTickFmt,
      formatXTooltip: v => v === 0 ? 'Race week' : `${-v} weeks to go`,
      formatY: v => fmtNum(v,0), formatTooltip: v => fmtNum(v,1) + ' km',
      height: 300, ariaLabel: 'Weekly kilometres by build'
    });
  }

  // 2. Cumulative volume
  {
    const cum = {};
    const series = buildsAll.map(b => {
      let total = 0;
      const pts = weeksToDate(b).slice().sort((a,c) => c.weeks_out - a.weeks_out).map(w => {
        total += w.km;
        return { x: -w.weeks_out, y: +total.toFixed(1) };
      });
      cum[buildYear(b)] = pts;
      return { id: buildYear(b), label: buildYear(b) + (b.is_current ? ' (now)' : ''),
               color: colorFor(b), emphasis: !!b.is_current, points: pts };
    });
    // Carry the current build forward along the plan.
    const plannedCum = (DATA.projection && DATA.projection.remaining_weeks) || [];
    if (plannedCum.length) {
      const cur = buildsAll.find(b => b.is_current);
      const curPts = cur ? (cum[buildYear(cur)] || []) : [];
      let running = curPts.length ? curPts[curPts.length - 1].y : 0;
      const startX = curPts.length ? curPts[curPts.length - 1].x : -DATA.build_weeks;
      const pts = [{ x: startX, y: running }];
      plannedCum.slice().sort((a, b) => b.weeks_out - a.weeks_out).forEach(w => {
        if (-w.weeks_out <= startX) return;
        running += w.km;
        pts.push({ x: -w.weeks_out, y: +running.toFixed(1) });
      });
      if (pts.length > 1) series.push({ id: 'planned', label: 'planned', color: cssVar('--series-4'),
                                        dashed: true, points: pts });
    }
    const rows = [];
    for (let w = DATA.build_weeks - 1; w >= 0; w--) {
      rows.push([xTickFmt(-w), ...buildsAll.map(b => {
        const hit = cum[buildYear(b)].find(p => p.x === -w);
        return hit ? fmtNum(hit.y,0) : null;
      })]);
    }
    const cumLegend = legend.slice();
    if (plannedCum.length) cumLegend.push({ label: 'planned (Runna)', color: cssVar('--series-4'), line: true });
    const { card: c, plot } = card('Cumulative kilometres in the block',
      'How much total work each build had banked at the same point.',
      { legend: cumLegend, table: makeTable(['Week', ...buildsAll.map(b => buildYear(b))], rows) });
    cards.appendChild(c);
    lineChart(plot, { series, xTicks: xTicksBuild, formatXTick: xTickFmt,
      formatXTooltip: v => v === 0 ? 'Race week' : `${-v} weeks to go`,
      formatY: v => fmtNum(v,0), formatTooltip: v => fmtNum(v,0) + ' km',
      ariaLabel: 'Cumulative kilometres by build' });
  }

  // 3. Long run
  {
    const rows = [];
    for (let w = DATA.build_weeks - 1; w >= 0; w--) {
      rows.push([xTickFmt(-w), ...buildsAll.map(b => {
        const hit = visibleWeeks(b).find(x => x.weeks_out === w);
        return hit && hit.long_run_km ? fmtNum(hit.long_run_km,1) : null;
      })]);
    }
    const plannedLong = (DATA.projection && DATA.projection.planned_weeks) || [];
    const longSeries = buildSeries(w => w.long_run_km || null, { dots: true });
    const longLegend = legend.slice();
    if (plannedLong.length) {
      const cur = buildsAll.find(b => b.is_current);
      const pts = plannedLong.filter(w => w.long_run_km > 0).map(w => ({ x: -w.weeks_out, y: w.long_run_km }));
      longSeries.push({ id: 'planned', label: 'planned', color: cssVar('--series-4'),
                        dashed: true, dots: true, points: pts.sort((a, b) => a.x - b.x) });
      longLegend.push({ label: 'planned (Runna)', color: cssVar('--series-4'), line: true });
    }
    const { card: c, plot } = card('Long run progression',
      'The longest single run of each week — the strongest predictor of a comfortable half.',
      { legend: longLegend, table: makeTable(['Week', ...buildsAll.map(b => buildYear(b))], rows) });
    cards.appendChild(c);
    lineChart(plot, {
      series: longSeries,
      xTicks: xTicksBuild, formatXTick: xTickFmt,
      formatXTooltip: v => v === 0 ? 'Race week' : `${-v} weeks to go`,
      formatY: v => fmtNum(v,0), formatTooltip: v => fmtNum(v,1) + ' km',
      ariaLabel: 'Longest run per week by build' });
  }

  // 4. Intensity mix
  {
    const ord = ['--ord-1','--ord-2','--ord-3'].map(cssVar);
    const rows = buildsAll.map(b => {
      const sum = k => b.weeks.reduce((a,w) => a + (w[k] || 0), 0);
      return { label: buildYear(b) + (b.is_current ? ' (now)' : ''),
               segments: [
                 { label: 'Easy', value: sum('easy_km'), color: ord[0], ink: '#0b0b0b' },
                 { label: 'Moderate', value: sum('moderate_km'), color: ord[1] },
                 { label: 'Hard', value: sum('hard_km'), color: ord[2] }
               ] };
    });
    // Projected row: what the mix becomes if the remaining plan is completed.
    const bands = DATA.projection && DATA.projection.planned_bands;
    if (bands) {
      const cur = rows[rows.length - 1];
      rows.push({
        label: '2026 projected',
        projected: true,
        segments: [
          { label: 'Easy', value: cur.segments[0].value + bands.easy, color: ord[0], ink: '#0b0b0b' },
          { label: 'Moderate', value: cur.segments[1].value + bands.moderate, color: ord[1] },
          { label: 'Hard', value: cur.segments[2].value + bands.hard, color: ord[2] }
        ]
      });
    }
    const tableRows = rows.map(r => {
      const total = r.segments.reduce((a,s) => a + s.value, 0) || 1;
      return [r.label, ...r.segments.map(s => `${fmtNum(s.value,0)} km (${fmtNum(100*s.value/total,0)}%)`)];
    });
    const { card: c, plot } = card('Intensity mix per build',
      'Share of kilometres by effort. Most half-marathon plans want roughly 80% easy. '
      + (bands ? 'The last row adds the remaining prescribed sessions to what you have already run.' : ''),
      { legend: rows[0].segments.map(s => ({ label: s.label, color: s.color })), wide: true,
        table: makeTable(['Build','Easy','Moderate','Hard'], tableRows) });
    cards.appendChild(c);
    stackedBars(plot, { rows, ariaLabel: 'Intensity distribution per build' });
  }
}

/* 5. Fitness & fatigue */
if (DATA.fitness && DATA.fitness.length) {
  const f = DATA.fitness;
  const rows = f.filter((_,i) => i % 7 === 0).map(d => [d.date, fmtNum(d.ctl), fmtNum(d.atl), fmtNum(d.tsb)]);
  const { card: c, plot } = card('Fitness and fatigue',
    'CTL is your 6-week rolling load (fitness), ATL the 7-day one (fatigue). Both in the same training-load units.',
    { legend: [{ label: 'Fitness (CTL)', color: cssVar('--series-1'), line: true },
               { label: 'Fatigue (ATL)', color: cssVar('--series-2'), line: true }],
      table: makeTable(['Date','CTL','ATL','TSB'], rows) });
  cards.appendChild(c);
  const toX = d => new Date(d).getTime();

  // Every past race, so the earlier build-ups are identifiable on the long view.
  const raceMarks = (DATA.races || [])
    .filter(r => !r.planned)
    .map(r => ({ x: toX(r.date), label: r.date.slice(0, 4) }));

  const RANGES = [
    { label: '3 months', days: 92 },
    { label: '1 year', days: 365 },
    { label: 'All builds', days: null }
  ];
  let activeRange = 1;

  const rangeBar = el('div', 'range');
  plot.parentNode.insertBefore(rangeBar, plot);

  const drawFitness = () => {
    const days = RANGES[activeRange].days;
    const cutoff = days ? Date.now() - days * 86400000 : -Infinity;
    const slice = f.filter(d => toX(d.date) >= cutoff);
    lineChart(plot, {
      series: [
        { id:'ctl', label:'CTL', color: cssVar('--series-1'), emphasis: true, area: true,
          points: slice.map(d => ({ x: toX(d.date), y: d.ctl })) },
        { id:'atl', label:'ATL', color: cssVar('--series-2'),
          points: slice.map(d => ({ x: toX(d.date), y: d.atl })) }
      ],
      marks: raceMarks,
      formatY: v => fmtNum(v,0), formatTooltip: v => fmtNum(v,1),
      formatXTick: v => new Date(v).toLocaleDateString('en-GB',{month:'short', year:'2-digit'}),
      formatXTooltip: v => new Date(v).toLocaleDateString('en-GB',{day:'numeric',month:'short',year:'numeric'}),
      height: 280, ariaLabel: 'Fitness and fatigue over time'
    });
  };

  // The Form chart shares the range, so both cards always show the same span.
  let drawForm = null;
  const redraw = () => { drawFitness(); if (drawForm) drawForm(); };

  RANGES.forEach((r, i) => {
    const b = el('button', null, r.label);
    b.setAttribute('aria-pressed', String(i === activeRange));
    b.addEventListener('click', () => {
      activeRange = i;
      rangeBar.querySelectorAll('button').forEach((other, j) =>
        other.setAttribute('aria-pressed', String(j === i)));
      redraw();
    });
    rangeBar.appendChild(b);
  });

  // 6. Form (TSB) — diverging around zero
  const { card: c2, plot: p2 } = card('Form (TSB = fitness − fatigue)',
    'Positive means fresh, negative means loaded. Race day usually feels best between +5 and +25.',
    { legend: [{ label: 'Fresh', color: cssVar('--pos') }, { label: 'Loaded', color: cssVar('--neg') }],
      table: makeTable(['Date','TSB'], rows.map(r => [r[0], r[3]])) });
  cards.appendChild(c2);
  drawForm = () => {
  const days = RANGES[activeRange].days;
  const cutoff = days ? Date.now() - days * 86400000 : -Infinity;
  lineChart(p2, {
    series: [{ id:'tsb', label:'TSB', color: cssVar('--pos'), emphasis: true,
               points: f.filter(d => d.tsb != null && toX(d.date) >= cutoff)
                        .map(d => ({ x: toX(d.date), y: d.tsb })) }],
    zeroLine: true, yPad: true, yMin: null, directLabels: false, padRight: 12,
    marks: raceMarks,
    formatY: v => fmtNum(v,0), formatTooltip: v => (v >= 0 ? '+' : '') + fmtNum(v,1),
    formatXTick: v => new Date(v).toLocaleDateString('en-GB',{month:'short', year:'2-digit'}),
    formatXTooltip: v => new Date(v).toLocaleDateString('en-GB',{day:'numeric',month:'short',year:'numeric'}),
    ariaLabel: 'Training stress balance'
  });
  };
  redraw();
}

/* 7. Aerobic efficiency */
if (DATA.efficiency && DATA.efficiency.length > 3) {
  const e = DATA.efficiency;
  const toX = d => new Date(d).getTime();
  const rolling = [];
  const win = Math.min(8, Math.max(3, Math.round(e.length / 8)));
  for (let i = win - 1; i < e.length; i++) {
    const slice = e.slice(i - win + 1, i + 1);
    rolling.push({ x: toX(e[i].date), y: +(slice.reduce((a,d) => a + d.ef, 0) / slice.length).toFixed(4) });
  }
  const rows = e.slice(-40).reverse().map(d => [d.date, fmtNum(d.distance_km,1), fmtPace(d.pace_s_per_km), fmtNum(d.average_heartrate,0), fmtNum(d.ef,3)]);
  // A trend line through a handful of points scattered over years would draw a
  // straight edge across multi-month gaps and imply continuity that isn't there.
  const enoughForTrend = e.length >= 12;
  const spanDays = (new Date(e[e.length-1].date) - new Date(e[0].date)) / 86400000;
  const legend = [{ label: 'Single run', color: cssVar('--series-3') }];
  if (enoughForTrend) legend.push({ label: `${win}-run average`, color: cssVar('--series-1'), line: true });
  const { card: c, plot } = card('Aerobic efficiency',
    enoughForTrend
      ? 'Metres per minute per heartbeat on easy and steady runs. Rising means the same pace is costing you fewer beats.'
      : `Metres per minute per heartbeat. Only ${e.length} of your runs recorded heart rate, spread over ${Math.round(spanDays/30)} months, so these are shown as individual points — too few and too scattered to draw a trend through.`,
    { legend, wide: true, table: makeTable(['Date','km','Pace','Avg HR','EF'], rows) });
  cards.appendChild(c);
  const efSeries = [
    { id:'ef', label:'runs', color: cssVar('--series-3'), dotsOnly: true, points: e.map(d => ({ x: toX(d.date), y: d.ef })) }
  ];
  if (enoughForTrend) {
    efSeries.push({ id:'avg', label:'trend', color: cssVar('--series-1'), emphasis: true, points: rolling });
  }
  lineChart(plot, {
    series: efSeries,
    yMin: Math.min(...e.map(d => d.ef)) * 0.96,
    yMax: Math.max(...e.map(d => d.ef)) * 1.04,
    formatY: v => fmtNum(v,2), formatTooltip: v => fmtNum(v,3),
    formatXTick: v => new Date(v).toLocaleDateString('en-GB',{month:'short', year:'2-digit'}),
    formatXTooltip: v => new Date(v).toLocaleDateString('en-GB',{day:'numeric',month:'short',year:'numeric'}),
    height: 280, ariaLabel: 'Aerobic efficiency over time'
  });
}

/* 8. Best efforts */
if (DATA.best_efforts && Object.keys(DATA.best_efforts).length) {
  const names = ['5k','10k','Half-Marathon'].filter(n => DATA.best_efforts[n] && DATA.best_efforts[n].length);
  if (names.length) {
    const toX = d => new Date(d).getTime();
    const series = names.map((n, i) => {
      // Running best: the progression line, not every effort.
      let best = Infinity;
      const pts = DATA.best_efforts[n].map(d => {
        best = Math.min(best, d.time_s);
        return { x: toX(d.date), y: best };
      });
      return { id: n, label: n === 'Half-Marathon' ? 'HM' : n, color: cssVar(SERIES_VARS[i]),
               emphasis: n === 'Half-Marathon', points: pts };
    });
    const rows = names.map(n => {
      const list = DATA.best_efforts[n];
      const best = list.reduce((a,d) => d.time_s < a.time_s ? d : a, list[0]);
      return [n, list.length, fmtDur(best.time_s), best.date, fmtPace(best.time_s / ({'5k':5,'10k':10,'15k':15,'Half-Marathon':21.0975}[n]))];
    });
    const { card: c, plot } = card('Personal best progression',
      'Your fastest split at each distance over time, taken from every run — it only ever steps down. One panel per distance, because a 5k and a half share no useful scale.',
      { wide: true, table: makeTable(['Distance','Efforts','Best','Date','Pace'], rows) });
    cards.appendChild(c);
    // Small multiples: separate panels beat cramming 4x-different times onto one axis.
    const grid = el('div','multiples');
    plot.appendChild(grid);
    series.forEach(s => {
      const panel = el('div','multiple');
      const head = el('h3');
      head.innerHTML = `<span class="swatch line" style="background:${s.color}"></span>${s.id}`;
      panel.appendChild(head);
      const sub = el('div');
      panel.appendChild(sub);
      grid.appendChild(panel);
      const ys = s.points.map(p => p.y);
      lineChart(sub, {
        series: [s], directLabels: false, padRight: 14, height: 170,
        yMin: Math.min(...ys) * 0.97, yMax: Math.max(...ys) * 1.03, yTickCount: 3,
        formatY: v => fmtDur(v), formatTooltip: v => fmtDur(v),
        formatXTick: v => new Date(v).toLocaleDateString('en-GB',{month:'short', year:'2-digit'}),
        formatXTooltip: v => new Date(v).toLocaleDateString('en-GB',{day:'numeric',month:'short',year:'numeric'}),
        ariaLabel: `Best ${s.id} progression` });
    });
  }
}

/* Race projection from the imported plan */
if (proj) {
  const s = proj.scenarios, cal = proj.calibration;
  const labels = {
    hold: ['Current fitness, no further gain', 'Today’s 5k carried across using the exponent your past races actually produced.'],
    plan_completed: ['You finish the plan', 'Every remaining session done: 5k about 1.5% faster and endurance a touch better for the extra volume.'],
    optimistic: ['Plan plus a good day', '5k 3% faster and a clear endurance gain — the top of what this block can reasonably give.']
  };
  const rows = Object.keys(labels).map(k => [labels[k][0], fmtDur(s[k]),
    proj.best_previous_s ? (proj.best_previous_s - s[k] > 0 ? '−' + fmtDur(proj.best_previous_s - s[k]) : '+' + fmtDur(s[k] - proj.best_previous_s)) : '–',
    fmtPace(s[k] / 21.0975)]);
  const { card: c, plot } = card('Half marathon projection',
    `From your ${proj.baseline.name} of ${fmtDur(proj.baseline.time_s)} (${proj.baseline.date}), scaled with an endurance exponent of ${cal.exponent} — ${cal.source}, not the textbook 1.06.`,
    { wide: true });
  cards.appendChild(c);

  const wrap = el('div','scroll');
  wrap.appendChild(makeTable(['Scenario','Finish','vs PB','Pace'], rows));
  plot.appendChild(wrap);

  const calRows = cal.points.map(p => [
    p.race, p.from, fmtDur(p.from_time_s), fmtDur(p.race_time_s), p.exponent,
    p.used ? 'used' : (p.note || 'excluded')
  ]);
  const det = el('div','tableview scroll');
  det.hidden = true;
  det.appendChild(makeTable(['Race','From','Short effort','Actual half','Exponent','Status'], calRows));
  c.appendChild(det);

  const note = el('p','hint',
    `Block volume if you finish: <strong>${fmtNum(proj.block_km)} km</strong> (${fmtNum(proj.done_km)} done + ${fmtNum(proj.planned_km)} to come), ` +
    `longest run ${fmtNum(proj.longest_planned_km,0)} km, ${proj.sessions_left} sessions left.`);
  note.style.marginTop = '10px';
  c.appendChild(note);
}

/* Prescribed vs actual */
if (DATA.compliance && DATA.compliance.length) {
  const done = DATA.compliance.filter(r => r.status === 'done' && r.delta_s != null);
  const rows = DATA.compliance.map(r => {
    const target = r.target_pace_s ? fmtPace(r.target_pace_s) + '/km'
                 : (r.easy_ceiling_s ? '≤ ' + fmtPace(r.easy_ceiling_s) + '/km' : 'conversational');
    const actual = r.segment_pace_s ? fmtPace(r.segment_pace_s) + '/km'
                 : (r.actual_pace_s ? fmtPace(r.actual_pace_s) + '/km' : '–');
    const delta = r.delta_s == null ? '–'
                : (r.delta_s > 0 ? '+' : '−') + Math.abs(r.delta_s) + 's';
    return [r.date, r.title, r.target_seg_km ? fmtNum(r.target_seg_km, 1) + ' km' : 'whole run',
            target, actual, delta];
  });

  const { card: c, plot } = card('Prescribed pace vs what you ran',
    'Each quality session is judged on the fastest continuous stretch of the matching length, so a '
    + 'warm-up never drags the average down. Bars run left when you were faster than prescribed, '
    + 'right when slower — for interval targets faster is overcooked, and on easy runs faster is the '
    + 'classic mistake.',
    { wide: true,
      legend: [{ label: 'faster than prescribed', color: cssVar('--pos') },
               { label: 'slower than prescribed', color: cssVar('--neg') }],
      table: makeTable(['Date','Session','Judged on','Target','Actual','Δ'], rows) });
  cards.appendChild(c);

  // Diverging bars around a zero rule.
  const host = el('div');
  plot.appendChild(host);
  const drawDeltas = () => {
    host.innerHTML = '';
    const W = Math.max(320, host.clientWidth || 640);
    const rowH = 30, gap = 10, padL = 168, padR = 56, padT = 6, padB = 24;
    const H = padT + padB + done.length * (rowH + gap);
    const iw = W - padL - padR;
    const max = Math.max(30, ...done.map(r => Math.abs(r.delta_s)));
    const mid = padL + iw / 2;
    const svg = svgEl('svg', { class: 'plot', viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: 'img' });
    svg.setAttribute('aria-label', 'Prescribed versus actual pace');

    done.forEach((r, i) => {
      const y = padT + i * (rowH + gap);
      const lbl = svgEl('text', { x: padL - 12, y: y + rowH / 2 + 4, 'text-anchor': 'end', class: 'lbl' });
      lbl.setAttribute('fill', cssVar('--text-secondary'));
      lbl.textContent = r.title.length > 26 ? r.title.slice(0, 25) + '…' : r.title;
      svg.appendChild(lbl);

      const w = Math.abs(r.delta_s) / max * (iw / 2);
      const faster = r.delta_s < 0;
      const rect = svgEl('rect', {
        x: faster ? mid - w : mid + 1, y, width: Math.max(1, w - 1), height: rowH, rx: 4,
        fill: faster ? cssVar('--pos') : cssVar('--neg')
      });
      svg.appendChild(rect);

      // Long bars would push an outside label back over the session name, so
      // the value moves inside once the bar is wide enough to hold it.
      const inside = w > 54;
      const val = svgEl('text', {
        x: faster ? (inside ? mid - w + 8 : mid - w - 8) : (inside ? mid + w - 8 : mid + w + 8),
        y: y + rowH / 2 + 4,
        'text-anchor': faster === inside ? 'start' : 'end'
      });
      val.setAttribute('fill', inside ? '#fcfcfb' : cssVar('--text-secondary'));
      if (inside) val.setAttribute('font-weight', '600');
      val.textContent = (r.delta_s > 0 ? '+' : '−') + Math.abs(r.delta_s) + 's/km';
      svg.appendChild(val);
    });

    svg.appendChild(svgEl('line', { x1: mid, x2: mid, y1: padT, y2: padT + done.length * (rowH + gap) - gap,
                                    stroke: cssVar('--axis'), 'stroke-width': 1 }));
    const zero = svgEl('text', { x: mid, y: H - 6, 'text-anchor': 'middle' });
    zero.setAttribute('fill', cssVar('--muted'));
    zero.textContent = 'on target';
    svg.appendChild(zero);
    host.appendChild(svg);
  };
  drawDeltas();
  new ResizeObserver(drawDeltas).observe(host);
}

/* Build comparison + recent runs tables */
const tables = document.getElementById('tables');
if (buildsAll.length) {
  tables.appendChild(el('h2','section-title','Build-by-build comparison'));
  const rows = buildsAll.map(b => {
    const t = b.totals || {};
    const race = b.race || {};
    return [
      buildYear(b) + (b.is_current ? ' <span class="pill">in progress</span>' : ''),
      race.time_s ? fmtDur(race.time_s) : '–',
      fmtNum(t.km,0), fmtNum(t.avg_week_km,1), fmtNum(t.peak_week_km,0),
      fmtNum(t.longest_run_km,1), fmtNum(t.runs_per_week,1),
      t.easy_share == null ? '–' : fmtNum(t.easy_share,0) + '%',
      t.avg_hr == null ? '–' : fmtNum(t.avg_hr,0),
      fmtNum(t.elev_m,0)
    ];
  });
  const wrap = el('div','scroll');
  wrap.appendChild(makeTable(
    ['Build','Race time','Total km','Avg/week','Peak week','Longest','Runs/week','Easy share','Avg HR','Elev m'], rows));
  tables.appendChild(wrap);
}

if (DATA.recent_runs && DATA.recent_runs.length) {
  tables.appendChild(el('h2','section-title','Recent runs'));
  const rows = DATA.recent_runs.map(r => [
    r.date, r.name, fmtNum(r.km,2), fmtDur(r.time_s), fmtPace(r.pace_s), r.hr ?? '–',
    r.intensity === 'unknown' ? '–' : r.intensity, fmtNum(r.load,0)
  ]);
  const wrap = el('div','scroll');
  wrap.appendChild(makeTable(['Date','Run','km','Time','Pace','HR','Effort','Load'], rows));
  tables.appendChild(wrap);
}

/* Methodology */
const phys = DATA.physiology || {};
const cal = (DATA.projection && DATA.projection.calibration) || {};
const corrected = (DATA.recent_runs || []).length;  // presence check only
const notes = [
  `Threshold heart rate ${phys.threshold_hr ?? '–'} bpm and max ${phys.max_hr ?? '–'} bpm — ${(phys.source||{}).threshold_hr || 'not set'}. Threshold pace ${phys.threshold_pace_s_per_km ? fmtPace(phys.threshold_pace_s_per_km) + '/km' : '–'}, ${(phys.source||{}).threshold_pace || 'not set'}. Override any of them in profile.json.`,

  `<strong>Intensity is measured as distance in zone, not by labelling whole runs.</strong> Where a session was prescribed (Runna plan or the workout text in the Strava description), each segment is banded by its target pace, so a 13 km run with 6 km at race pace counts as 6 km of quality and 7 km easy — not 13 km of quality. Unstructured runs still take one label for the whole run, which is correct for them.`,

  `Bands: easy below 83% of threshold pace, moderate 83–97%, hard above; by heart rate, easy below 88% of threshold HR, moderate 88–99%, hard above. One fixed threshold is used across all years so the comparison reflects how you distributed effort rather than how your fitness moved. Set "threshold_follows_fitness": true in profile.json to use a per-year threshold instead.`,

  `A recorded RPE (1–10) overrides both the band and the load for that run — that is how sessions on a curved manual treadmill, in heat, or on rough ground get counted honestly, since their pace understates the work by 20–30%.`,

  `Training load per run = hours × intensity² × 100. CTL (fitness) and ATL (fatigue) are exponential averages over 42 and 7 days, both starting from zero at the first recorded day, so the opening weeks of the chart are the metric warming up rather than real fitness.`,

  `Build windows are ${DATA.build_weeks} weeks ending on race week; the race itself is excluded from past build totals so training volume stays comparable. Runs whose recording stopped early are repaired from the distance stated in the title, and the time is scaled with them so the pace is preserved.`,

  `The projected half time uses Riegel (t₂ = t₁ × (d₂/d₁)^k) with <strong>k = ${cal.exponent ?? '1.06'}</strong> — ${cal.source || 'textbook default'}, not the textbook 1.06. Your own races say you lose more over distance than the average runner, so a generic exponent would flatter the projection by several minutes.`
];
document.getElementById('methodology').innerHTML = notes.map(n => `<li>${n}</li>`).join('');

/* Controls */
const tablesToggle = document.getElementById('tables-toggle');
tablesToggle.addEventListener('click', () => {
  const on = tablesToggle.getAttribute('aria-pressed') === 'true';
  tablesToggle.setAttribute('aria-pressed', String(!on));
  tablesToggle.textContent = on ? 'Show data tables' : 'Hide data tables';
  document.querySelectorAll('.tableview').forEach(t => t.hidden = on);
});

// Absent in the hosted build, where the viewer supplies its own theme control.
const themeToggle = document.getElementById('theme-toggle');
if (themeToggle) {
  const applyTheme = t => {
    if (t === 'auto') document.documentElement.removeAttribute('data-theme');
    else document.documentElement.setAttribute('data-theme', t);
    themeToggle.textContent = t === 'auto' ? 'Theme: auto' : (t === 'dark' ? 'Theme: dark' : 'Theme: light');
    localStorage.setItem('theme', t);
    window.dispatchEvent(new Event('resize'));
  };
  let theme = localStorage.getItem('theme') || 'auto';
  applyTheme(theme);
  themeToggle.addEventListener('click', () => {
    theme = theme === 'auto' ? 'light' : theme === 'light' ? 'dark' : 'auto';
    applyTheme(theme);
    setTimeout(() => location.reload(), 30);
  });
}
</script>
</body>
</html>
"""


def _clean(value):
    """NaN/Infinity are not valid JSON - JSON.parse would throw on them."""
    if isinstance(value, float):
        return None if (math.isnan(value) or math.isinf(value)) else value
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


WEB_PATH = DASHBOARD_PATH.with_name("dashboard-web.html")


def _as_fragment(html: str) -> str:
    """Strip the document wrapper for hosting, where the host supplies its own.

    Also drops the page's theme button: the viewer stamps data-theme itself, and
    two controls writing the same attribute fight each other.
    """
    import re as _re

    head = _re.search(r"<title>.*?</style>", html, _re.S)
    body = _re.search(r"<body>(.*)</body>", html, _re.S)
    if not head or not body:
        return html
    inner = body.group(1).replace(
        '<button class="ctl" id="theme-toggle">Theme</button>', "")
    return head.group(0) + "\n" + inner


def build_dashboard(open_browser: bool = True, web: bool = False):
    payload = _clean(build_payload())
    # </script> inside the JSON would close the tag early.
    encoded = json.dumps(payload, allow_nan=False, default=str).replace("</", "<\\/")
    html = TEMPLATE.replace("__DATA__", encoded)
    DASHBOARD_PATH.write_text(html, encoding="utf-8")
    if web:
        WEB_PATH.write_text(_as_fragment(html), encoding="utf-8")
    if open_browser:
        webbrowser.open(DASHBOARD_PATH.as_uri())
    return WEB_PATH if web else DASHBOARD_PATH
