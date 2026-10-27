// real input events (CDP), no Playwright actionability probes; optional injected CSS; N = clone factor, THR = CPU throttle
const { chromium } = require('playwright');
const BASE = process.env.ML_BASE || 'http://127.0.0.1:8199/', PW = process.env.ML_PW || 'pw-bench';
(async () => {
  const N = +process.argv[2] || 1, THR = +process.argv[3] || 1, CSS = process.argv[4] || '';
  const b = await chromium.launch();
  const ctx = await b.newContext({ extraHTTPHeaders: {"Tailscale-User-Login": process.env.ML_LOGIN || "me@x"}, viewport:{width:1500,height:900}});
  const p = await ctx.newPage(); const cdp = await ctx.newCDPSession(p); await cdp.send('Performance.enable');
  p.on('pageerror', e => console.log('PAGEERR', e.message));
  await p.goto(BASE); await p.waitForSelector('#grid .book, table.list tbody tr');
  await p.evaluate(async pw => { const r = await api.unlock(pw); SESSION.unlockedUntil = r.expiresAt; render(); }, PW);
  await p.click('#kind-book'); if (await p.locator('#lay-grid[aria-pressed=false]').count()) await p.click('#lay-grid'); await p.waitForSelector('#grid .book');
  if (N > 1) await p.evaluate(n => { const o = LIB.items.slice(); const out=[]; for (let k=0;k<n;k++) for (const i of o) out.push(k ? {...i, id: i.id+'_'+k} : i); LIB.items = out;
    const sh = LIB.shelves.find(s => s.id === st.shelf); sh.items = LIB.items.filter(i => i.kind === 'book').map(i => i.id); render(); }, N);
  if (CSS) await p.addStyleTag({content: CSS});
  if (process.env.JS) await p.evaluate(process.env.JS);
  await p.waitForTimeout(1500);
  if (THR > 1) await cdp.send('Emulation.setCPUThrottlingRate', {rate: THR});
  const frame = () => p.evaluate(() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r))));
  const m = async () => Object.fromEntries((await cdp.send('Performance.getMetrics')).metrics.map(x => [x.name, x.value]));
  const click = async sel => {
    const box = await p.evaluate(s => { const e = typeof s === 'string' ? document.querySelector(s) : null; if (!e) return null; e.scrollIntoView({block: 'nearest'}); const r = e.getBoundingClientRect(); return {x: r.left + r.width / 2, y: r.top + Math.min(r.height / 2, 40)}; }, sel);
    if (!box) return 'missing';
    await cdp.send('Input.dispatchMouseEvent', {type: 'mouseMoved', x: box.x, y: box.y}); await frame();
    const a = await m(), t0 = Date.now();
    await cdp.send('Input.dispatchMouseEvent', {type: 'mousePressed', x: box.x, y: box.y, button: 'left', clickCount: 1});
    await cdp.send('Input.dispatchMouseEvent', {type: 'mouseReleased', x: box.x, y: box.y, button: 'left', clickCount: 1});
    await frame();
    const z = await m(), d = k => ((z[k] - a[k]) * 1000).toFixed(0).padStart(5);
    return `wall ${String(Date.now() - t0).padStart(5)} ms | task ${d('TaskDuration')} script ${d('ScriptDuration')} style ${d('RecalcStyleDuration')} layout ${d('LayoutDuration')}`;
  };
  const steps = [['size L', '#size-l'], ['size M', '#size-m'], ['layout list', '#lay-list'], ['layout grid', '#lay-grid'], ['Select on', '#sel-toggle'],
    ['select cover a', '#grid .book:nth-child(3)'], ['select cover b', '#grid .book:nth-child(4)'], ['Select off', '#sel-toggle'], ['chip Read', '#f-completed'], ['chip All', '#f-all'],
    ['open detail', '#grid .book:nth-child(6)'], ['close detail', '#d-close']];
  console.log('items', await p.evaluate(() => LIB.items.length), 'throttle', THR, CSS ? 'css: ' + CSS.slice(0, 60) : '');
  for (const [label, sel] of steps) { console.log(label.padEnd(16), await click(sel)); if (label === 'open detail') await p.waitForTimeout(2000); }
  await b.close();
})();
