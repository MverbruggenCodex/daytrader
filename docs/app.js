/* Daytrader desk — reads the engine export from the repo and live prices from Hyperliquid. */
(() => {
  const DATA = 'https://raw.githubusercontent.com/MverbruggenCodex/daytrader/main/export/';
  const HL = 'https://api.hyperliquid.xyz/info';
  const MARKETS = { BTCUSD: 'BTC', XAUUSD: 'xyz:GOLD', EURUSD: 'xyz:EUR', SP500: 'xyz:SP500' };
  const NAMES = { BTCUSD: 'Bitcoin', XAUUSD: 'Goud', EURUSD: 'Euro / dollar', SP500: 'S&P 500' };
  const SESSIONS = [
    { name: 'london', tz: 'Europe/London', open: [8, 0], entryUntil: [11, 0], close: [16, 0], fill: 'var(--lon)' },
    { name: 'newyork', tz: 'America/New_York', open: [9, 30], entryUntil: [12, 0], close: [16, 0], fill: 'var(--ny)' },
  ];
  const H = 3600e3;

  const $ = (id) => document.getElementById(id);
  const nf = (d) => new Intl.NumberFormat('nl-NL', { minimumFractionDigits: d, maximumFractionDigits: d });
  const usd = new Intl.NumberFormat('nl-NL', { style: 'currency', currency: 'USD' });
  const px = (v) => (v == null ? '—' : nf(Math.abs(v) < 10 ? 4 : Math.abs(v) < 1000 ? 2 : 1).format(v));
  const pct = (v) => (v > 0 ? '+' : '') + nf(2).format(v) + '%';
  const amsTime = new Intl.DateTimeFormat('nl-NL', { hour: '2-digit', minute: '2-digit', timeZone: 'Europe/Amsterdam' });
  const amsDay = new Intl.DateTimeFormat('nl-NL', { weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', timeZone: 'Europe/Amsterdam' });
  const el = (tag, attrs = {}, text) => { const e = document.createElement(tag); for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v); if (text != null) e.textContent = text; return e; };
  const svgEl = (tag, attrs = {}) => { const e = document.createElementNS('http://www.w3.org/2000/svg', tag); for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v); return e; };
  const real = (rows) => (Array.isArray(rows) ? rows.filter((r) => !r._empty) : []);

  let exp = {};
  let candles = [];
  let lastPrices = {};

  async function getJSON(name) {
    try {
      const r = await fetch(DATA + name + '.json?t=' + Math.floor(Date.now() / 60000), { cache: 'no-store' });
      return r.ok ? await r.json() : null;
    } catch (e) { return null; }
  }

  async function hl(coin, interval, hours) {
    const now = Date.now();
    const r = await fetch(HL, { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ type: 'candleSnapshot', req: { coin, interval, startTime: now - hours * H, endTime: now } }) });
    const rows = await r.json();
    return rows.map((x) => ({ t: x.t, o: +x.o, h: +x.h, l: +x.l, c: +x.c }));
  }

  // UTC ms of a wall-clock time in a time zone on the calendar day of `ref`
  function zonedTime(ref, tz, hh, mm) {
    const parts = Object.fromEntries(new Intl.DateTimeFormat('en-US', { timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit' })
      .formatToParts(ref).filter((p) => p.type !== 'literal').map((p) => [p.type, p.value]));
    const guess = Date.UTC(+parts.year, +parts.month - 1, +parts.day, hh, mm);
    const off = (t) => {
      const p = Object.fromEntries(new Intl.DateTimeFormat('en-US', { timeZone: tz, hourCycle: 'h23', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
        .formatToParts(t).filter((q) => q.type !== 'literal').map((q) => [q.type, q.value]));
      return Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour, +p.minute) - t;
    };
    return guess - off(guess - off(guess));
  }

  /* ---- chart ---------------------------------------------------------------------- */
  function drawChart() {
    const box = $('chart');
    box.replaceChildren();
    if (!candles.length) { box.append(el('div', { class: 'msg' }, 'Koersen konden niet geladen worden. Probeer het over een minuut opnieuw.')); return; }
    const W = box.clientWidth, Hh = box.clientHeight;
    const pad = { l: 8, r: 64, t: 12, b: 24 };
    const t0 = candles[0].t, t1 = candles[candles.length - 1].t + 5 * 60e3;
    const zones = real(exp.zones_chart).filter((z) => z.market === 'BTCUSD' && Date.parse(z.until || new Date()) >= t0);
    const sess = real(exp.sessions).filter((s) => s.market === 'BTCUSD' && Date.parse(s.open) >= t0 - 2 * H);
    const trades = real(exp.trades).filter((t) => t.market === 'BTCUSD' && (t.status === 'Open' || Date.parse(t.closed_at || 0) >= t0));
    let lo = Math.min(...candles.map((c) => c.l)), hi = Math.max(...candles.map((c) => c.h));
    for (const t of trades.filter((t) => t.status === 'Open')) { lo = Math.min(lo, t.stop, t.target); hi = Math.max(hi, t.stop, t.target); }
    const padY = (hi - lo) * 0.06; lo -= padY; hi += padY;
    const x = (t) => pad.l + ((t - t0) / (t1 - t0)) * (W - pad.l - pad.r);
    const y = (v) => pad.t + (1 - (v - lo) / (hi - lo)) * (Hh - pad.t - pad.b);
    const svg = svgEl('svg', { viewBox: `0 0 ${W} ${Hh}`, preserveAspectRatio: 'none' });

    // session bands
    for (let d = -1; d <= 1; d++) {
      const ref = new Date(t1 + d * 24 * H);
      for (const s of SESSIONS) {
        const a = zonedTime(ref, s.tz, ...s.open), b = zonedTime(ref, s.tz, ...s.close);
        if (b < t0 || a > t1) continue;
        const xa = x(Math.max(a, t0)), xb = x(Math.min(b, t1));
        svg.append(svgEl('rect', { x: xa, y: 0, width: Math.max(0, xb - xa), height: Hh, fill: s.fill }));
      }
    }
    // zones
    for (const z of zones) {
      const a = Math.max(Date.parse(z.from), t0), b = z.until ? Date.parse(z.until) : t1;
      const dem = z.side === 'long';
      svg.append(svgEl('rect', { x: x(a), y: y(z.top), width: Math.max(2, x(b) - x(a)), height: Math.max(1, y(z.bottom) - y(z.top)),
        fill: dem ? 'var(--dem)' : 'var(--sup)', stroke: dem ? 'var(--dem-line)' : 'var(--sup-line)', 'stroke-width': 1 }));
    }
    // opening ranges
    for (const s of sess) {
      const cfg = SESSIONS.find((q) => q.name === s.session);
      const a = Date.parse(s.open), b = Date.parse(s.range_end);
      const until = cfg ? zonedTime(new Date(a), cfg.tz, ...cfg.entryUntil) : b;
      const xa = x(Math.max(a, t0)), xb = x(b), xe = x(Math.min(until, t1));
      svg.append(svgEl('rect', { x: xa, y: y(s.or_high), width: Math.max(1, xb - xa), height: Math.max(1, y(s.or_low) - y(s.or_high)), fill: 'none', stroke: 'var(--or)', 'stroke-width': 1.5 }));
      for (const v of [s.or_high, s.or_low]) svg.append(svgEl('line', { x1: xb, x2: xe, y1: y(v), y2: y(v), stroke: 'var(--or)', 'stroke-width': 1, 'stroke-dasharray': '4 4', opacity: 0.7 }));
    }
    // candles
    const bw = Math.max(1, ((W - pad.l - pad.r) / candles.length) * 0.62);
    for (const c of candles) {
      const col = c.c >= c.o ? 'var(--up)' : 'var(--down)';
      const cx = x(c.t + 2.5 * 60e3);
      svg.append(svgEl('line', { x1: cx, x2: cx, y1: y(c.h), y2: y(c.l), stroke: col, 'stroke-width': 1 }));
      svg.append(svgEl('rect', { x: cx - bw / 2, y: y(Math.max(c.o, c.c)), width: bw, height: Math.max(1, Math.abs(y(c.o) - y(c.c))), fill: col }));
    }
    // trades
    for (const t of trades) {
      const long = t.side === 'Long';
      const xo = x(Math.max(Date.parse(t.opened_at), t0));
      if (t.status === 'Open') {
        for (const [v, col] of [[t.stop, 'var(--down)'], [t.target, 'var(--up)'], [t.entry, 'var(--ink)']]) {
          svg.append(svgEl('line', { x1: xo, x2: W - pad.r, y1: y(v), y2: y(v), stroke: col, 'stroke-width': 1.5, 'stroke-dasharray': '6 4' }));
        }
      }
      const yo = y(t.entry), s = 7;
      svg.append(svgEl('path', { d: long ? `M${xo},${yo + s} l${-s},${s * 1.4} h${2 * s}z` : `M${xo},${yo - s} l${-s},${-s * 1.4} h${2 * s}z`, fill: 'var(--ink)' }));
      if (t.exit != null && t.closed_at) {
        const xc = x(Date.parse(t.closed_at)), win = (t.pnl_usd || 0) > 0;
        svg.append(svgEl('circle', { cx: xc, cy: y(t.exit), r: 5, fill: win ? 'var(--up)' : 'var(--down)', stroke: 'var(--sheet)', 'stroke-width': 2 }));
      }
    }
    // price axis + last price
    const step = niceStep((hi - lo) / 5);
    for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) {
      const tl = svgEl('text', { x: W - pad.r + 8, y: y(v) + 4, fill: 'var(--muted)', 'font-size': 11 }); tl.textContent = px(v); svg.append(tl);
    }
    for (let k = 0; k <= 24; k += 6) {
      const tt = t1 - (24 - k) * H; if (tt < t0 || x(tt) < 24 || x(tt) > W - pad.r - 12) continue;
      const tl = svgEl('text', { x: x(tt), y: Hh - 6, fill: 'var(--muted)', 'font-size': 11, 'text-anchor': 'middle' }); tl.textContent = amsTime.format(tt); svg.append(tl);
    }
    const last = candles[candles.length - 1];
    svg.append(svgEl('line', { x1: pad.l, x2: W - pad.r, y1: y(last.c), y2: y(last.c), stroke: 'var(--ink)', 'stroke-width': 1, opacity: 0.35 }));
    const tag = svgEl('rect', { x: W - pad.r + 2, y: y(last.c) - 10, width: pad.r - 4, height: 20, rx: 3, fill: 'var(--ink)' }); svg.append(tag);
    const tagT = svgEl('text', { x: W - pad.r + 8, y: y(last.c) + 4, fill: 'var(--paper)', 'font-size': 11, 'font-weight': 600 }); tagT.textContent = px(last.c); svg.append(tagT);

    // crosshair + tooltip
    const cross = svgEl('line', { y1: 0, y2: Hh, stroke: 'var(--ink)', 'stroke-width': 1, opacity: 0, 'stroke-dasharray': '2 3' });
    svg.append(cross);
    box.append(svg);
    const tip = el('div', { class: 'tip' }); box.append(tip);
    const move = (ev) => {
      const r = box.getBoundingClientRect();
      const mx = ev.clientX - r.left;
      const t = t0 + ((mx - pad.l) / (W - pad.l - pad.r)) * (t1 - t0);
      let best = candles[0];
      for (const c of candles) if (Math.abs(c.t - t) < Math.abs(best.t - t)) best = c;
      const cx = x(best.t + 2.5 * 60e3);
      cross.setAttribute('x1', cx); cross.setAttribute('x2', cx); cross.setAttribute('opacity', 0.6);
      tip.replaceChildren(el('div', {}, amsTime.format(best.t)), el('div', {}, `O ${px(best.o)}  H ${px(best.h)}`), el('div', {}, `L ${px(best.l)}  S ${px(best.c)}`));
      tip.style.display = 'block';
      const left = cx + 12 + tip.offsetWidth > W ? cx - tip.offsetWidth - 12 : cx + 12;
      tip.style.left = left + 'px'; tip.style.top = '10px';
    };
    box.onpointermove = move; box.onpointerdown = move;
    box.onpointerleave = () => { tip.style.display = 'none'; cross.setAttribute('opacity', 0); };
  }

  function niceStep(raw) {
    const p = Math.pow(10, Math.floor(Math.log10(raw)));
    const n = raw / p;
    return (n < 1.5 ? 1 : n < 3 ? 2 : n < 7 ? 5 : 10) * p;
  }

  /* ---- panels --------------------------------------------------------------------- */
  function drawHeader() {
    const a = (exp.account || [])[0] || {};
    const mode = $('mode');
    if (a.liquid_paper_mode === false) { mode.textContent = 'Let op: Liquid staat niet in paper modus'; mode.classList.add('live'); }
    else mode.textContent = 'Paper account';
    $('sync').textContent = a.updated ? `Laatste ronde ${amsDay.format(new Date(a.updated))}, koersen live` : 'Koersen live';
  }

  function drawNumbers() {
    const a = (exp.account || [])[0] || {};
    const start = Number(a.start_balance) || 10000;
    const eq = a.liquid_equity != null ? Number(a.liquid_equity) : null;
    $('n-equity').textContent = eq == null ? '—' : usd.format(eq);
    $('n-equity-s').textContent = a.liquid_synced_at ? `Gecontroleerd ${amsDay.format(new Date(a.liquid_synced_at))}` : '';
    if (eq != null) {
      const res = eq - start;
      const v = $('n-result'); v.textContent = (res > 0 ? '+' : '') + usd.format(res);
      v.className = 'v ' + (res > 0 ? 'up' : res < 0 ? 'down' : '');
      $('n-result-s').textContent = `${pct((eq / start - 1) * 100)} op ${usd.format(start)}`;
    }
    const open = real(exp.trades).filter((t) => t.status === 'Open');
    $('n-open').textContent = open.length ? `${open[0].side} ${open[0].market}` : 'Geen';
    $('n-open-s').textContent = open.length ? `entry ${px(open[0].entry)}, stop ${px(open[0].stop)}, doel ${px(open[0].target)}` : `Max ${a.risk_pct ?? 1}% risico per trade`;
    const wait = real(exp.orders).filter((o) => o.status === 'Wacht op bevestiging');
    $('n-wait').textContent = wait.length ? `${wait.length} order${wait.length > 1 ? 's' : ''}` : 'Niets';
    $('n-wait-s').textContent = wait.length ? 'Bevestig in de Claude-app' : 'Je krijgt een melding bij een signaal';
  }

  let feedLimit = 8;
  function drawFeed() {
    const list = $('feed');
    list.replaceChildren();
    const rows = real(exp.analyses).slice().sort((a, b) => Date.parse(b.time) - Date.parse(a.time));
    const kind = (r) => (r.trigger === 'Entry' ? 'k-entry' : r.trigger === 'Exit' ? 'k-exit ' + (/winst/.test(r.title) ? 'win' : 'loss') : r.trigger === 'Nieuwe zone' ? 'k-zone' : '');
    for (const r of rows.slice(0, feedLimit)) {
      const li = el('li', { class: kind(r) });
      li.append(el('p', { class: 'when' }, `${amsDay.format(new Date(r.time))}, ${r.market}`), el('p', { class: 'what' }, r.title));
      if (r.text) li.append(el('p', { class: 'why' }, r.text));
      if (r.action) li.append(el('p', { class: 'act' }, r.action));
      list.append(li);
    }
    if (rows.length > feedLimit) {
      const b = el('button', { class: 'more', type: 'button' }, `Toon ${Math.min(10, rows.length - feedLimit)} eerdere`);
      b.onclick = () => { feedLimit += 10; drawFeed(); };
      list.append(b);
    }
  }

  async function drawMarkets() {
    const ul = $('markets');
    const rows = real(exp.markets).filter((m) => MARKETS[m.market]);
    const items = await Promise.all(rows.map(async (m) => {
      let cs = [];
      try { cs = await hl(MARKETS[m.market], '1h', 48); } catch (e) { /* keep last price from export */ }
      return { m, cs };
    }));
    ul.replaceChildren();
    for (const { m, cs } of items) {
      const last = cs.length ? cs[cs.length - 1].c : m.price;
      const prev = cs.length > 24 ? cs[cs.length - 25].c : null;
      lastPrices[m.market] = last;
      const li = el('li');
      const name = el('p', { class: 'm' }, NAMES[m.market] || m.market);
      const trading = m.trading === true;
      name.append(el('span', { class: 'tag ' + (trading ? 'on' : 'off') }, trading ? 'handelt' : 'volgt'));
      const ch = prev ? (last / prev - 1) * 100 : null;
      li.append(name, el('p', { class: 'p' }, px(last)), el('p', { class: 'st' }, m.note || m.status),
        el('p', { class: 'ch ' + (ch > 0 ? 'up' : ch < 0 ? 'down' : '') }, ch == null ? '' : pct(ch) + ' in 24 uur'));
      if (cs.length > 1) {
        const w = 300, h = 34, mn = Math.min(...cs.map((c) => c.c)), mx = Math.max(...cs.map((c) => c.c));
        const s = svgEl('svg', { viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: 'none', 'aria-hidden': 'true' });
        const d = cs.map((c, k) => `${k ? 'L' : 'M'}${(k / (cs.length - 1)) * w},${h - 2 - ((c.c - mn) / (mx - mn || 1)) * (h - 4)}`).join('');
        s.append(svgEl('path', { d, fill: 'none', stroke: ch >= 0 ? 'var(--up)' : 'var(--down)', 'stroke-width': 1.5, 'vector-effect': 'non-scaling-stroke' }));
        li.append(s);
      }
      ul.append(li);
    }
  }

  function drawTrades() {
    const tb = document.querySelector('#trades tbody');
    tb.replaceChildren();
    const rows = real(exp.trades).slice().sort((a, b) => Date.parse(b.opened_at) - Date.parse(a.opened_at));
    $('trades-empty').hidden = rows.length > 0;
    for (const t of rows) {
      const tr = el('tr');
      const res = t.pnl_usd == null ? (t.status === 'Open' ? 'Open' : '—') : usd.format(t.pnl_usd);
      const cells = [t.id, t.market, t.side, amsDay.format(new Date(t.opened_at)), px(t.entry), px(t.stop), px(t.target), '1:' + nf(2).format(t.rr), res, t.liquid || '—'];
      cells.forEach((v, k) => {
        const td = el('td', k >= 4 && k <= 8 ? { class: 'n' } : {}, v);
        if (k === 8 && t.pnl_usd != null) td.classList.add(t.pnl_usd > 0 ? 'up' : 'down');
        tr.append(td);
      });
      tb.append(tr);
    }
  }

  async function refreshPrices() {
    try {
      candles = await hl('BTC', '5m', 24);
      const last = candles[candles.length - 1].c, first = candles[0].o;
      const p = $('btc-price');
      p.textContent = px(last); p.classList.remove('tick'); void p.offsetWidth; p.classList.add('tick');
      const ch = (last / first - 1) * 100;
      const c = $('btc-change'); c.textContent = `${pct(ch)} in 24 uur`; c.className = 'change ' + (ch >= 0 ? 'up' : 'down');
      document.title = `BTC ${px(last)} | Daytrader desk`;
    } catch (e) { candles = candles || []; }
    drawChart();
  }

  async function refreshData() {
    const names = ['account', 'trades', 'analyses', 'markets', 'orders', 'sessions', 'zones_chart'];
    const got = await Promise.all(names.map(getJSON));
    names.forEach((n, k) => { if (got[k]) exp[n] = got[k]; });
    drawHeader(); drawNumbers(); drawFeed(); drawTrades();
    await drawMarkets();
    drawChart();
  }

  let rt;
  window.addEventListener('resize', () => { clearTimeout(rt); rt = setTimeout(drawChart, 120); });
  refreshData().then(refreshPrices);
  setInterval(refreshPrices, 60e3);
  setInterval(refreshData, 5 * 60e3);
})();
