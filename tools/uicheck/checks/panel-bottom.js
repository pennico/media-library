// The info panel's last line (Move to trash) can be scrolled into view: the panel fits under the top bar and the sticky
// page bar on a wide screen (also with active filters making the bar taller, and with the page scrolled), and the phone
// sheet fits in the window.
const { chromium } = require('playwright');
const BASE = process.env.ML_BASE || 'http://127.0.0.1:8199/', PW = process.env.ML_PW || 'pw-bench';
let fails = 0; const ok = (c, m) => { if (!c) fails++; console.log(c ? 'PASS' : 'FAIL', m); };
(async () => {
  const b = await chromium.launch();
  const ctx = await b.newContext({ extraHTTPHeaders: {"Tailscale-User-Login": process.env.ML_LOGIN || "me@x"}, viewport: {width: 1400, height: 800}, reducedMotion: 'reduce' });
  const p = await ctx.newPage(); const errs = []; p.on('pageerror', e => errs.push(e.message));
  await p.goto(BASE); await p.waitForFunction(() => LIB.shelves.length && document.querySelector('#kinds [aria-current=true]'));
  await p.evaluate(async pw => { const r = await api.unlock(pw); SESSION.unlockedUntil = r.expiresAt; render(); }, PW);
  await p.click('#kind-book'); await p.click('#lay-grid');
  const ids = await p.evaluate(() => [...document.querySelectorAll('#grid > .book')].map(e => e.dataset.id));
  const bottom = async label => {
    await p.waitForSelector('#d-trash');
    const r = await p.evaluate(() => { const d = document.getElementById('detail'); d.scrollTop = d.scrollHeight;
      const t = document.getElementById('d-trash').getBoundingClientRect(), box = d.getBoundingClientRect();
      return {trash: Math.round(t.bottom), panel: Math.round(box.bottom), vh: innerHeight, top: Math.round(box.top)}; });
    await p.waitForTimeout(50);
    ok(r.panel <= r.vh && r.trash <= r.vh, `${label}: the panel ends in the window and Move to trash can be reached ${JSON.stringify(r)}`);
  };
  await p.click('#b-' + ids[0]); await p.waitForFunction(i => st.detail && st.detail.id === i, ids[0]);
  await bottom('wide screen, page at its top');
  await p.evaluate(() => scrollTo(0, 2000)); await p.waitForTimeout(150);
  await bottom('wide screen, page scrolled (the page bar stuck)');
  await p.setViewportSize({width: 1000, height: 700}); await p.waitForTimeout(200);
  await bottom('narrower window, the page bar on two rows');
  await p.setViewportSize({width: 375, height: 700}); await p.waitForTimeout(300);
  await bottom('phone sheet');
  ok(errs.length === 0, 'no page errors' + (errs.length ? ': ' + errs.join(' | ') : ''));
  console.log(fails ? fails + ' FAILED' : 'ALL PASS');
  await b.close();
})();
