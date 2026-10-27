const { chromium } = require('playwright');
let fails = 0; const ok = (c, m) => { if (!c) fails++; console.log(c ? 'PASS' : 'FAIL', m); };
const BASE = process.env.ML_BASE || 'http://127.0.0.1:8199/', PW = process.env.ML_PW || 'pw-bench';
const unlock = p => p.evaluate(async pw => { const r = await api.unlock(pw); SESSION.unlockedUntil = r.expiresAt; render(); }, PW);
const books = async p => { await p.click('#kind-book'); if (await p.locator('#lay-grid[aria-pressed=false]').count()) await p.click('#lay-grid'); await p.waitForSelector('#grid .book'); };
(async () => {
  const b = await chromium.launch();
  const ctx = await b.newContext({ extraHTTPHeaders: {"Tailscale-User-Login": process.env.ML_LOGIN || "me@x"}, viewport:{width:1400,height:900}, deviceScaleFactor: 2});
  const p = await ctx.newPage(); const errs = []; p.on('pageerror', e => errs.push(e.message));
  await p.goto(BASE); await p.waitForSelector('#grid .book, table.list tbody tr'); await unlock(p); await books(p);
  // toasts: a stack of three at most, each can be dismissed
  await p.evaluate(() => { for (let k = 1; k <= 5; k++) toast('message ' + k); });
  ok(await p.locator('#toasts .toast').count() === 3 && (await p.evaluate(() => lastToast())) === 'message 5', 'toasts stack, three at most, newest last');
  await p.locator('#toasts .toast .tx').first().click(); ok(await p.locator('#toasts .toast').count() === 2, 'a toast can be dismissed');
  await p.evaluate(() => { $('toasts').innerHTML = ''; });
  // Undo a trash from the bar
  await p.click('#sel-toggle'); const ids = await p.evaluate(() => [...document.querySelectorAll('#grid > .book')].slice(1, 3).map(b => b.dataset.id));
  for (const id of ids) await p.click(`#b-${id}`);
  const tb = await p.locator('#toasts').boundingBox(), bar = await p.locator('#bulkbar').boundingBox();
  await p.click('#bulkbar [data-bulk=trash]'); await p.waitForTimeout(600);
  ok(ids.every(id => true) && await p.evaluate(ids => ids.every(id => !document.getElementById('b-' + id)), ids), 'Move to trash: both covers leave the shelf');
  ok((await p.evaluate(() => lastToast())).startsWith('Moved to the trash') && await p.locator('#toasts .tact').count() === 1, 'the toast offers Undo');
  await p.locator('#toasts .tact').click(); await p.waitForTimeout(800);
  ok(await p.evaluate(ids => ids.every(id => !!document.getElementById('b-' + id) && !LIB.items.find(i => i.id === id).section), ids), 'Undo brings both back to the shelf');
  // Undo an archive puts the book back where it was
  await p.click(`#b-${ids[0]}`); await p.click('#bulkbar [data-bulk=archive]'); await p.waitForTimeout(600);
  ok(await p.evaluate(id => !!LIB.items.find(i => i.id === id).section, ids[0]), 'Archive moves it to the Archive');
  await p.locator('#toasts .tact').last().click(); await p.waitForTimeout(800);
  ok(await p.evaluate(id => !LIB.items.find(i => i.id === id).section && !!document.getElementById('b-' + id), ids[0]), 'Undo puts it back in the library');
  await p.keyboard.press('Escape'); await p.click('#sel-toggle').catch(() => {}); await p.evaluate(() => { if (st.selecting) { st.selecting = false; st.sel.clear(); renderDatabase(); } });
  // srcset: sizes from the real column width; a sharp choice at DPR 2
  const ss = await p.evaluate(() => { const im = document.querySelector('#grid .cov img[srcset]'); return [!!im, im?.sizes, document.getElementById('grid').dataset.sizes]; });
  ok(ss[0] && ss[1] === ss[2] && /^\d+px$/.test(ss[1]), 'covers carry srcset; sizes = the drawn width (' + ss[1] + ')');
  await p.click('#size-l'); await p.waitForTimeout(1500);
  const cur = await p.evaluate(() => document.querySelector('#grid .cov img[srcset]').currentSrc);
  ok(/-(480|640)\.jpg$/.test(cur), 'L covers on a 2x screen load the 480/640 px picture: ' + cur.split('/').pop());
  await p.click('#size-m');
  // keyboard: Enter on a cover opens it and puts focus in the panel; Back closes it
  await p.locator('#grid .book').nth(4).focus(); await p.keyboard.press('Enter'); await p.waitForTimeout(2300);
  ok(await p.evaluate(() => document.activeElement?.id === 'd-title'), 'opening a book from the keyboard moves focus to its title in the panel');
  const sel = await p.evaluate(() => st.selected);
  ok(await p.evaluate(id => history.state?.ml === id && location.hash === '#item=' + encodeURIComponent(id), sel), 'the open book is in the address (#item=…)');
  // the flight face: once decoded, the 640 px picture
  await p.locator('#grid .book').nth(4).click({force: true}).catch(() => {});
  await p.goBack(); await p.waitForTimeout(500);
  ok(await p.evaluate(() => !st.selected && document.getElementById('detail').hidden), 'Back closes the panel');
  await p.evaluate(() => { const b = document.querySelectorAll('#grid > .book')[7]; b.click(); });
  let face = ''; for (let k = 0; k < 12 && !/640/.test(face); k++) { await p.waitForTimeout(100); face = await p.evaluate(() => document.querySelector('.flybook .fb-front img')?.src || ''); }
  ok(/-640\.jpg$/.test(face), 'the flying cover switches to the 640 px picture: ' + face.split('/').pop());
  await p.waitForTimeout(1600);
  const id7 = await p.evaluate(() => st.selected);
  await p.reload(); await p.waitForSelector('#grid .book'); await p.waitForTimeout(1500);
  ok(await p.evaluate(id => st.selected === id && !document.getElementById('detail').hidden, id7), 'a reload opens the same book again');
  await p.click('#d-close'); await p.waitForTimeout(400);
  // the library from the last visit: drawn before the server answers; offline it stays
  const p2 = await ctx.newPage(); let release; const gate = new Promise(r => release = r);
  await p2.route('**/api/library', async r => { await gate; await r.continue(); });
  const t0 = Date.now(); await p2.goto(BASE); await p2.waitForSelector('#grid .book', {timeout: 5000});
  const early = Date.now() - t0;
  ok(await p2.evaluate(() => !!document.querySelector('#grid .book') && OFFLINE_COPY), `a return visit draws the last library before the server answers (${early} ms)`);
  release(); await p2.waitForTimeout(800);
  ok(await p2.evaluate(() => !OFFLINE_COPY && !SESSION.connecting), 'then the server\'s copy takes over');
  await p2.close();
  const p3 = await ctx.newPage(); await p3.route('**/api/**', r => r.abort());
  await p3.goto(BASE); await p3.waitForSelector('#grid .book', {timeout: 5000}); await p3.waitForTimeout(500);
  ok((await p3.textContent('#keybar')).includes('Offline'), 'without the server: the last copy, marked offline, with Try again');
  await p3.close();
  // Keep unlocked
  await p.evaluate(() => { SESSION.unlockedUntil = Date.now() / 1000 + 120; renderKeybar(); });
  await p.click('#key-extend'); await p.fill('#key-input', PW); await p.click('#key-save'); await p.waitForTimeout(500);
  ok(await p.evaluate(() => SESSION.unlockedUntil * 1000 - Date.now() > 25 * 60e3), '"Keep unlocked" renews the session for 30 minutes');
  // fit a column of a long (windowed) list from all its rows
  await p.evaluate(n => { const o = LIB.items.slice(); const out=[]; for (let k=0;k<n;k++) for (const i of o) out.push(k ? {...i, id: i.id+'_'+k, title: k === 9 && i === o[100] ? 'An exceptionally long title that only sits far down the list, never drawn at first' : i.title} : i); LIB.items = out;
    const sh = LIB.shelves.find(s => s.id === st.shelf); sh.items = LIB.items.filter(i => i.kind === 'book').map(i => i.id); render(); }, 10);
  await p.click('#lay-list'); await p.waitForTimeout(300);
  await p.evaluate(() => document.querySelector('th[data-ck=title] .colgrip').dispatchEvent(new MouseEvent('dblclick', {bubbles: true}))); await p.waitForTimeout(200);
  const fit = await p.evaluate(() => { const th = document.querySelector('th[data-ck=title]'), c = document.createElement('canvas').getContext('2d'), cs = getComputedStyle(document.querySelector('table.list .ltitle b'));
    c.font = `${cs.fontWeight} ${cs.fontSize} ${cs.fontFamily}`; return [parseFloat(th.style.width), c.measureText('An exceptionally long title that only sits far down the list, never drawn at first').width]; });
  ok(fit[0] >= fit[1], `fit sizes the title column for a title far down the list (${fit[0]} >= ${Math.round(fit[1])})`);
  await p.close();
  // phone: the sheet has a backdrop that closes it; its ✕ stays in view
  const ph = await b.newContext({ extraHTTPHeaders: {"Tailscale-User-Login": process.env.ML_LOGIN || "me@x"}, viewport:{width:390,height:844}, deviceScaleFactor: 3, isMobile: true, hasTouch: true});
  const q = await ph.newPage(); q.on('pageerror', e => errs.push('phone: ' + e.message));
  await q.goto(BASE); await q.waitForSelector('#grid .book, table.list tbody tr');
  await q.evaluate(() => { document.getElementById('kind-book')?.click(); if (document.querySelector('#lay-grid[aria-pressed=false]')) document.getElementById('lay-grid').click(); });
  await q.waitForSelector('#grid .book'); await q.evaluate(() => document.querySelectorAll('#grid > .book')[2].click()); await q.waitForTimeout(2300);
  ok(await q.evaluate(() => document.getElementById('scrim').classList.contains('on') && getComputedStyle(document.getElementById('scrim')).display !== 'none'), 'phone: the open book is a sheet over a backdrop');
  await q.evaluate(() => { const d = document.getElementById('detail'); d.scrollTop = d.scrollHeight; }); await q.waitForTimeout(200);
  ok(await q.evaluate(() => { const d = document.getElementById('detail').getBoundingClientRect(), x = document.getElementById('d-close').getBoundingClientRect(); return x.top >= d.top - 1 && x.bottom <= d.top + 60; }), 'phone: scrolled to the end of the sheet, its ✕ is still at the top');
  await q.mouse.click(195, 60); await q.waitForTimeout(400);
  ok(await q.evaluate(() => !st.selected && !document.getElementById('scrim').classList.contains('on')), 'phone: a tap on the backdrop closes the sheet');
  await ph.close();
  ok(errs.length === 0, 'no page errors' + (errs.length ? ': ' + errs.join(' | ') : ''));
  console.log(fails ? fails + ' FAILED' : 'ALL PASS');
  await b.close();
})();
