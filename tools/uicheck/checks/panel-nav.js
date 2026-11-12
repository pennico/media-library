// D71: the panel moves through the list the shelf shows: ‹ › and "3 of 12", ← → keys, the shelf's arrow keys take the
// panel along, the address follows without new history entries, an item that leaves the filtered list keeps its
// neighbours, the list layout too, and the phone sheet.
const { chromium } = require('playwright');
const BASE = process.env.ML_BASE || 'http://127.0.0.1:8199/', PW = process.env.ML_PW || 'pw-bench';
let fails = 0; const ok = (c, m) => { if (!c) fails++; console.log(c ? 'PASS' : 'FAIL', m); };
(async () => {
  const b = await chromium.launch();
  const ctx = await b.newContext({ extraHTTPHeaders: {"Tailscale-User-Login": process.env.ML_LOGIN || "me@x"}, viewport: {width: 1400, height: 900} });
  const p = await ctx.newPage(); const errs = []; p.on('pageerror', e => errs.push(e.message));
  await p.goto(BASE); await p.waitForFunction(() => LIB.shelves.length && document.querySelector('#kinds [aria-current=true]'));
  await p.click('#kind-book'); await p.evaluate(() => { st.listSort = null; }); await p.click('#lay-grid'); await p.selectOption('#sort', 'title');
  const cards = () => p.evaluate(() => [...document.querySelectorAll('#grid > .book')].map(e => e.dataset.id));
  const ids = await cards(), N = ids.length;
  const pos = () => p.evaluate(() => document.querySelector('.pn-pos')?.textContent || '');
  const opened = id => p.waitForFunction(i => st.selected === i && st.detail && st.detail.id === i, id);
  await p.emulateMedia({reducedMotion: 'reduce'});                   // no flight: the panel at once
  await p.locator('#grid > .book').nth(2).click(); await opened(ids[2]);
  ok(await pos() === `3 of ${N}`, `the panel says 3 of ${N}`);
  const h0 = await p.evaluate(() => history.length);
  await p.click('#pn-next'); await opened(ids[3]);
  ok(await pos() === `4 of ${N}`, 'Next: 4 of N');
  ok(await p.evaluate(i => document.getElementById('b-' + i).getAttribute('aria-current') === 'true', ids[3]), 'the shelf marks the item now open');
  ok(await p.evaluate(() => location.hash) === '#item=' + ids[3] && await p.evaluate(() => history.length) === h0, 'the address follows, no new history entry');
  ok(await p.evaluate(() => document.activeElement?.id) === 'pn-next', 'the focus stays on Next');
  await p.keyboard.press('ArrowLeft'); await opened(ids[2]);
  ok(await pos() === `3 of ${N}`, '← goes back to 3 of N');
  await p.keyboard.press('ArrowRight'); await p.keyboard.press('ArrowRight'); await opened(ids[4]);
  ok(await pos() === `5 of ${N}`, '→ → goes to 5 of N');
  // the ends
  await p.evaluate(i => openDetail(i), ids[0]); await opened(ids[0]);
  ok(await p.locator('#pn-prev').isDisabled() && !(await p.locator('#pn-next').isDisabled()), 'the first item: no Previous');
  await p.evaluate(i => openDetail(i), ids[N - 1]); await opened(ids[N - 1]);
  ok(await p.locator('#pn-next').isDisabled(), 'the last item: no Next');
  // the shelf's arrow keys take the open panel along
  await p.evaluate(i => openDetail(i), ids[1]); await opened(ids[1]);
  await p.locator('#b-' + ids[1]).focus(); await p.keyboard.press('ArrowRight'); await opened(ids[2]);
  ok(await p.evaluate(i => document.activeElement?.id === 'b-' + i, ids[2]), 'arrow on the shelf: the next cover has the focus and the panel shows it');
  // an item that leaves a filtered list keeps its neighbours (unlocked: mark it as read under To read)
  await p.evaluate(async pw => { const r = await api.unlock(pw); SESSION.unlockedUntil = r.expiresAt; render(); }, PW);
  await p.click('#d-close'); await p.click('#f-planned');
  const plan = await cards();
  if (plan.length >= 3) {
    await p.evaluate(i => openDetail(i), plan[1]); await opened(plan[1]);
    ok(await pos() === `2 of ${plan.length}`, `To read: 2 of ${plan.length}`);
    await p.click('#st-completed'); await p.waitForFunction(i => st.detail?.id === i && st.detail.status === 'completed', plan[1]);
    ok(!(await cards()).includes(plan[1]), 'marked read: it left the To read shelf');
    ok(await pos() === `2 of ${plan.length}`, 'the panel still knows where it was in the list');
    await p.click('#pn-next'); await opened(plan[2]);
    ok(true, 'Next opens the To read item that came after it');
    await p.evaluate(async i => { await api.status(i, 'planned'); }, plan[1]);
  } else console.log('SKIP filtered list [fewer than 3 To read books]');
  await p.click('#f-all');
  // the list layout
  await p.click('#lay-list'); await p.waitForSelector('table.list');
  const rows = await p.evaluate(() => (LWIN ? LWIN.list : [...document.querySelectorAll('table.list tbody tr[data-id]')]).map(r => r.id || r.dataset.id));
  await p.evaluate(i => openDetail(i), rows[0]); await opened(rows[0]);
  await p.click('#pn-next'); await opened(rows[1]);
  ok(await pos() === `2 of ${rows.length}`, 'the list layout: Next goes down the rows');
  await p.click('#lay-grid');
  // phone: the sheet's top row with ‹ ›, nothing sideways
  await p.setViewportSize({width: 375, height: 760}); await p.waitForTimeout(300);
  await p.evaluate(i => openDetail(i), ids[3]); await opened(ids[3]);
  const ov = await p.evaluate(() => { const d = document.getElementById('detail'), t = d.querySelector('.dtop').getBoundingClientRect(); return {sw: d.scrollWidth, cw: d.clientWidth, page: document.documentElement.scrollWidth, vw: innerWidth, top: Math.round(t.top - d.getBoundingClientRect().top)}; });
  ok(ov.sw <= ov.cw + 1 && ov.page <= ov.vw + 1, `phone: the sheet fits 375 px ${JSON.stringify(ov)}`);
  await p.evaluate(() => { document.getElementById('detail').scrollTop = 600; }); await p.waitForTimeout(150);
  const stuck = await p.evaluate(() => { const d = document.getElementById('detail'), t = d.querySelector('.dtop').getBoundingClientRect(); return Math.round(t.top - d.getBoundingClientRect().top); });
  ok(stuck >= -1 && stuck <= 2, `phone: ‹ › and close stay at the top of the sheet when it scrolls (${stuck})`);
  if (process.env.SHOTS) await p.screenshot({path: process.env.SHOTS + '/nav-phone.png'});
  await p.click('#pn-next'); await opened(ids[4]);
  ok(await pos() === `5 of ${N}`, 'phone: Next works in the sheet');
  await p.setViewportSize({width: 1400, height: 900}); await p.waitForTimeout(200);
  if (process.env.SHOTS) await p.screenshot({path: process.env.SHOTS + '/nav-desktop.png'});
  ok(errs.length === 0, 'no page errors' + (errs.length ? ': ' + errs.join(' | ') : ''));
  console.log(fails ? fails + ' FAILED' : 'ALL PASS');
  await b.close();
})();
