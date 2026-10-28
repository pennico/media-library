// What the 1.0.0 review found, checked: history with links and Forward, the backdrop and forms, Undo (twice at once,
// only what the action did, reachable by keyboard and screen readers), the key bar's password, toasts above the bar.
const { chromium } = require('playwright');
const BASE = process.env.ML_BASE || 'http://127.0.0.1:8199/', PW = process.env.ML_PW || 'pw-bench', LOGIN = process.env.ML_LOGIN || 'me@x';
let fails = 0; const ok = (c, m) => { if (!c) fails++; console.log(c ? 'PASS' : 'FAIL', m); };
const unlock = p => p.evaluate(async pw => { const r = await api.unlock(pw); SESSION.unlockedUntil = r.expiresAt; render(); }, PW);
const books = async p => { await p.evaluate(() => { document.getElementById('kind-book')?.click(); if (document.querySelector('#lay-grid[aria-pressed=false]')) document.getElementById('lay-grid').click(); }); await p.waitForSelector('#grid .book'); };
(async () => {
  const b = await chromium.launch();
  const ctx = await b.newContext({ extraHTTPHeaders: {"Tailscale-User-Login": LOGIN}, viewport: {width: 1400, height: 900}});
  const errs = [];
  const p = await ctx.newPage(); p.on('pageerror', e => errs.push(e.message));
  await p.goto(BASE); await p.waitForSelector('#grid .book, table.list tbody tr'); await unlock(p); await books(p);
  const id = await p.evaluate(() => document.querySelectorAll('#grid > .book')[3].dataset.id);
  // a link into a book: closing it stays on the console
  const q = await ctx.newPage(); q.on('pageerror', e => errs.push(e.message));
  await q.goto('about:blank'); await q.goto(BASE + '#item=' + encodeURIComponent(id)); await q.waitForSelector('#grid .book'); await q.waitForTimeout(1500);
  ok(await q.evaluate(i => st.selected === i, id), 'a link into a book opens it');
  await q.click('#d-close'); await q.waitForTimeout(600);
  ok(q.url().startsWith(BASE) && await q.evaluate(() => !st.selected && !history.state?.ml), 'closing it stays on the console (no step back out of it)');
  await q.close();
  // Forward into a book from Visuals: the shelf and the panel show
  await p.evaluate(i => document.getElementById('b-' + i).click(), id); await p.waitForTimeout(2000);
  await p.click('#d-close'); await p.waitForTimeout(500);
  await p.click('.seg button[data-sec=visuals]'); await p.waitForTimeout(300);
  await p.goForward(); await p.waitForTimeout(800);
  ok(await p.evaluate(i => st.sec === 'database' && !document.getElementById('v-database').hidden && st.selected === i && !document.getElementById('detail').hidden, id), 'Forward into a book from Visuals shows its shelf and panel');
  // a kind switch with the panel open: the address no longer names the book; a reload does not reopen it
  await p.click('#kind-movie'); await p.waitForTimeout(300);
  ok(await p.evaluate(() => !history.state?.ml && !location.hash.startsWith('#item=')), 'switching kind clears the book from the address');
  await p.reload(); await p.waitForTimeout(1200);
  ok(await p.evaluate(() => !st.selected && st.kind === 'movie'), 'a reload then stays where it was (no old book)');
  await unlock(p); await books(p);
  // covers kept aside while the width changed (a chip) get the new drawn width when they come back
  await p.click('#f-completed'); await p.click('#size-l'); await p.waitForTimeout(400); await p.click('#f-all'); await p.waitForTimeout(400);
  ok(await p.evaluate(() => { const sz = document.getElementById('grid').dataset.sizes; return [...document.querySelectorAll('#grid .cov img[srcset]')].every(im => im.sizes === sz); }),
    'after a chip and a size change, every cover has the drawn width');
  await p.click('#size-m');
  // Undo: only what the action did, and two at once
  const [x, y] = await p.evaluate(() => [...document.querySelectorAll('#grid > .book')].slice(5, 7).map(e => e.dataset.id));
  await p.hover('#b-' + x); await p.click(`#b-${x} .selbox`); await p.click('#b-' + y);
  await p.evaluate(async i => { await api.trashItem(i); }, x);              // trashed meanwhile from somewhere else
  await p.click('#bulkbar [data-bulk=trash]'); await p.waitForTimeout(700);
  ok(await p.evaluate(() => document.activeElement?.classList.contains('tact')), 'after a bulk trash, focus is on Undo (the bar it came from is gone)');
  await p.locator('#toasts .tact').last().click(); await p.waitForTimeout(900);
  ok(await p.evaluate(([x, y]) => !LIB.items.some(i => i.id === x) && LIB.items.some(i => i.id === y), [x, y]), 'Undo brings back only what its action trashed');
  await p.evaluate(async i => { await api.restoreItem(i); LIB = await api.library(); render(); }, x);
  await p.evaluate(() => { st.sel.clear(); renderDatabase(); });
  const [a1, a2] = await p.evaluate(() => [...document.querySelectorAll('#grid > .book')].slice(8, 10).map(e => e.dataset.id));
  for (const i of [a1, a2]) { await p.evaluate(i => { st.sel = new Set([i]); renderDatabase(); }, i); await p.click('#bulkbar [data-bulk=trash]'); await p.waitForTimeout(500); }
  await p.evaluate(() => { const u = [...document.querySelectorAll('#toasts .tact')]; u.forEach(b => b.click()); });
  await p.waitForTimeout(1500);
  ok(await p.evaluate(([a, b]) => LIB.items.some(i => i.id === a) && LIB.items.some(i => i.id === b), [a1, a2]), 'two Undos clicked together both put their items back');
  ok(await p.evaluate(() => !document.getElementById('toasts').closest('[aria-hidden=true]')), 'the toasts (and their Undo) are not hidden from screen readers');
  await p.evaluate(() => { st.sel.clear(); renderDatabase(); });
  // the key bar keeps a password being typed when it is redrawn
  await p.evaluate(() => { SESSION.unlockedUntil = Date.now() / 1000 + 120; renderKeybar(); });
  await p.click('#key-extend'); await p.keyboard.type('half-typ');
  await p.evaluate(() => renderKeybar());
  ok(await p.evaluate(() => $('key-input')?.value === 'half-typ' && document.activeElement?.id === 'key-input'), 'a redraw of the key bar keeps the password being typed, and its focus');
  await p.fill('#key-input', PW); await p.click('#key-save'); await p.waitForTimeout(500);
  await p.close();
  // phone: the backdrop asks before throwing away a form; toasts sit above a wrapped selection bar
  const ph = await b.newContext({ extraHTTPHeaders: {"Tailscale-User-Login": LOGIN}, viewport: {width: 390, height: 844}, deviceScaleFactor: 3, isMobile: true, hasTouch: true});
  const m = await ph.newPage(); m.on('pageerror', e => errs.push('phone: ' + e.message));
  await m.goto(BASE); await m.waitForSelector('#grid .book, table.list tbody tr'); await unlock(m); await books(m);
  await m.evaluate(() => document.querySelectorAll('#grid > .book')[2].click()); await m.waitForTimeout(2300);
  await m.evaluate(() => document.getElementById('rv-edit')?.click() || document.querySelector('[id^=rv-]')?.click());
  await m.evaluate(() => { st.editReview = true; });
  let asked = false; m.once('dialog', d => { asked = true; d.dismiss(); });
  await m.mouse.click(195, 40); await m.waitForTimeout(300);
  ok(asked && await m.evaluate(() => !!st.selected), 'phone: a tap on the backdrop asks before closing over an open form (and stays when told no)');
  await m.evaluate(() => { st.editReview = false; closeDetail(); });
  await m.waitForTimeout(400);
  await m.evaluate(() => { st.sel = new Set([...document.querySelectorAll('#grid > .book')].slice(0, 3).map(e => e.dataset.id)); renderDatabase(); });
  await m.evaluate(() => toast('A message while the bar is up'));
  await m.waitForTimeout(300);
  const geo = await m.evaluate(() => { const t = document.querySelector('#toasts .toast').getBoundingClientRect(), bar = document.getElementById('bulkbar').getBoundingClientRect(); return [Math.round(t.bottom), Math.round(bar.top), Math.round(bar.height)]; });
  ok(geo[0] <= geo[1], `phone: a toast sits above the selection bar (${geo[2]} px tall): ${geo[0]} <= ${geo[1]}`);
  await ph.close();
  ok(errs.length === 0, 'no page errors' + (errs.length ? ': ' + errs.join(' | ') : ''));
  console.log(fails ? fails + ' FAILED' : 'ALL PASS');
  await b.close();
})();
