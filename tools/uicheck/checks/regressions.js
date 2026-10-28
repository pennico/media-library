const { chromium } = require('playwright');
const BASE = process.env.ML_BASE || 'http://127.0.0.1:8199/', PW = process.env.ML_PW || 'pw-bench';
let fails = 0; const ok = (c, m) => { if (!c) fails++; console.log(c ? 'PASS' : 'FAIL', m); };
(async () => {
  const b = await chromium.launch();
  const ctx = await b.newContext({ extraHTTPHeaders: {"Tailscale-User-Login": process.env.ML_LOGIN || "me@x"}, viewport:{width:1400,height:900}});
  const p = await ctx.newPage(); const errs = []; p.on('pageerror', e => errs.push(e.message));
  await p.goto(BASE); await p.waitForSelector('#grid .book, table.list tbody tr');
  await p.evaluate(async pw => { const r = await api.unlock(pw); SESSION.unlockedUntil = r.expiresAt; render(); }, PW);
  await p.click('#kind-book'); if (await p.locator('#lay-grid[aria-pressed=false]').count()) await p.click('#lay-grid');
  await p.waitForSelector('#grid .book');
  // row toggle after reversing the order selects what is on that row now
  await p.selectOption('#sort', 'title'); await p.click('#sortdir'); await p.waitForTimeout(200);
  await p.locator('#boards .rowsel').first().click();
  const r = await p.evaluate(() => { const cols = gridRows(document.getElementById('grid')).cols, kids = [...document.querySelectorAll('#grid > .book')];
    return [kids.slice(0, cols).every(b => b.classList.contains('sel')), document.querySelectorAll('#grid .book.sel').length, cols]; });
  ok(r[0] && r[1] === r[2], 'after flipping the sort, the first row toggle selects the first row as shown: ' + JSON.stringify(r));
  await p.click('#sel-none'); await p.selectOption('#sort', 'manual');
  // a full reload keeps the cards that did not change
  await p.evaluate(() => { document.querySelectorAll('#grid > .book').forEach(b => b.__k = 1); });
  await p.evaluate(async () => { LIB = await api.library(); renderDatabase(); });
  ok(await p.evaluate(() => [...document.querySelectorAll('#grid > .book')].every(b => b.__k === 1)), 'a full library reload keeps every unchanged card node');
  // a kept action is dropped when another book is open by unlock time
  await p.locator('#grid .book').nth(2).click(); await p.waitForTimeout(1900);
  const idA = await p.evaluate(() => st.selected), before = await p.evaluate(id => LIB.items.find(i => i.id === id).rating, idA);
  await p.evaluate(() => { SESSION.unlockedUntil = null; renderKeybar(); });
  await p.evaluate(id => act(async () => afterItemChange(await api.patch(id, {entry: {rating: 2}})), 'Rating saved'), idA);
  ok(await p.evaluate(() => !!st.pending), 'a change while locked is kept');
  await p.locator('#grid .book').nth(5).click(); await p.waitForTimeout(400);
  await p.fill('#key-input', PW); await p.keyboard.press('Enter'); await p.waitForTimeout(800);
  const after = await p.evaluate(async id => (await (await fetch('api/items/' + id)).json()).rating, idA);
  ok(after === before && (await p.evaluate(() => lastToast())).includes('moved on'), 'unlocking after moving to another book does not apply it (rating ' + before + ' -> ' + after + ')');
  await p.click('#d-close');
  // windowed list: a redraw deep in the list keeps the place; Tab reaches rows past the drawn ones
  await p.evaluate(n => { const o = LIB.items.slice(); const out=[]; for (let k=0;k<n;k++) for (const i of o) out.push(k ? {...i, id: i.id+'_'+k} : i); LIB.items = out;
    const sh = LIB.shelves.find(s => s.id === st.shelf); sh.items = LIB.items.filter(i => i.kind === 'book').map(i => i.id); render(); }, 10);
  await p.click('#lay-list'); await p.waitForTimeout(200);
  await p.evaluate(() => scrollTo(0, 600 * LWIN.rowH)); await p.waitForTimeout(200);
  const y0 = await p.evaluate(() => scrollY); await p.evaluate(() => render()); await p.waitForTimeout(200);
  const y1 = await p.evaluate(() => scrollY);
  ok(Math.abs(y1 - y0) < 2 && await p.evaluate(() => LWIN.from > 400), `a redraw deep in the list keeps the scroll (${y0} -> ${y1})`);
  await p.evaluate(() => scrollTo(0, 0)); await p.waitForTimeout(200);
  const lastDrawn = await p.evaluate(() => LWIN.to);
  await p.evaluate(() => document.querySelector('table.list tr[data-id] [data-ltitle]').focus());
  for (let k = 0; k < (lastDrawn + 10) * 3; k++) await p.keyboard.press('Tab');
  const fk = await p.evaluate(() => { const tr = document.activeElement.closest('tr[data-id]'); return tr ? LWIN.index.get(tr.dataset.id) : -1; });
  ok(fk > lastDrawn, `Tab goes past the rows first drawn (focus on row ${fk}, ${lastDrawn} were drawn)`);
  ok(await p.evaluate(() => document.querySelector('table.list.win').getAttribute('aria-rowcount') === String(LWIN.list.length + 1)), 'the table tells assistive tech how many rows it has');
  await p.evaluate(async () => { LIB = await api.library(); render(); }); await p.waitForTimeout(200);
  // undo after a redraw, drafts across a filter
  await p.click('#tbl-edit'); await p.waitForSelector('tr.editing');
  await p.evaluate(() => { G.a = G.f = [0, GCOLS.indexOf('pages')]; gpaste('111\n222'); });
  await p.evaluate(() => render());
  await p.locator('tr.editing').nth(0).locator('[data-f=title]').focus(); await p.keyboard.press('Control+z');
  ok(await p.evaluate(() => { const r = document.querySelectorAll('tr.editing'); return r[0].querySelector('[data-f=pages]').value !== '111' && r[1].querySelector('[data-f=pages]').value !== '222'; }), 'Ctrl+Z after a redraw undoes the paste on screen');
  const t0 = await p.evaluate(() => document.querySelectorAll('tr.editing')[0].querySelector('[data-f=title]').value);
  await p.locator('tr.editing').nth(0).locator('[data-f=title]').fill(t0 + ' (draft)');
  await p.click('#filter-q'); await p.keyboard.type('zzzz-none'); await p.waitForTimeout(350);
  await p.fill('#filter-q', ''); await p.waitForTimeout(350);
  ok(await p.evaluate(t => document.querySelectorAll('tr.editing')[0].querySelector('[data-f=title]').value === t, t0 + ' (draft)'), 'a draft hidden by the filter comes back with its row');
  p.once('dialog', d => d.accept()); await p.click('#tbl-cancel');
  ok(errs.length === 0, 'no page errors' + (errs.length ? ': ' + errs.join(' | ') : ''));
  console.log(fails ? fails + ' FAILED' : 'ALL PASS');
  await b.close();
})();
