const { chromium } = require('playwright');
const BASE = process.env.ML_BASE || 'http://127.0.0.1:8199/', PW = process.env.ML_PW || 'pw-bench';
let fails = 0; const ok = (c, m) => { if (!c) fails++; console.log(c ? 'PASS' : 'FAIL', m); };
(async () => {
  const b = await chromium.launch();
  const ctx = await b.newContext({ extraHTTPHeaders: {"Tailscale-User-Login": process.env.ML_LOGIN || "me@x"}, viewport:{width:1400,height:900}});
  const p = await ctx.newPage(); const errs = []; p.on('pageerror', e => errs.push(e.message));
  const patches = []; p.on('request', r => { if (r.method() === 'PATCH' && r.url().includes('/api/shelves/')) patches.push(r.postData()); });
  await p.goto(BASE); await p.waitForSelector('#grid .book, table.list tbody tr');
  await p.evaluate(async pw => { const r = await api.unlock(pw); SESSION.unlockedUntil = r.expiresAt; render(); }, PW);
  await p.click('#kind-book'); if (await p.locator('#lay-grid[aria-pressed=false]').count()) await p.click('#lay-grid');
  if (await p.locator('#size-m[aria-pressed=false]').count()) await p.click('#size-m');
  await p.waitForSelector('#grid .book'); await p.waitForTimeout(300);
  const nBooks = await p.evaluate(() => LIB.items.filter(i => i.kind === 'book' && !i.section).length);
  ok(await p.locator('#grid .book').count() === nBooks, `grid shows all ${nBooks} books`);
  // boards line up under the first row of covers
  const align = await p.evaluate(() => { const c = document.querySelector('#grid .book .cov').getBoundingClientRect(), bd = document.querySelector('#boards .board').getBoundingClientRect(); return Math.abs(bd.top - c.bottom); });
  ok(align <= 2, 'first board sits under the first row of covers (off by ' + align.toFixed(1) + ' px)');
  // sort: one PATCH with both fields, order changes
  patches.length = 0; const first0 = await p.locator('#grid .book').first().getAttribute('data-id');
  await p.selectOption('#sort', 'title'); await p.waitForTimeout(400);
  ok(patches.length === 1 && patches[0].includes('sortKey') && patches[0].includes('sortDesc'), 'sort change sends one PATCH with key and direction: ' + JSON.stringify(patches));
  await p.selectOption('#sort', 'manual'); await p.waitForTimeout(400);
  // size: class flips, cards reused (same nodes)
  const mark = await p.evaluate(() => { document.querySelector('#grid .book').__mark = 1; return 1; });
  await p.click('#size-l'); ok(await p.evaluate(() => document.getElementById('grid').classList.contains('l') && document.querySelector('#grid .book').__mark === 1), 'size L: class set, card nodes reused');
  await p.click('#size-m');
  // select mode
  await p.click('#sel-toggle');
  ok(await p.evaluate(() => getComputedStyle(document.querySelector('#grid .selbox')).display !== 'none'), 'select mode shows the check boxes');
  for (const k of [1, 2, 3]) await p.locator('#grid .book').nth(k).click();
  ok(await p.locator('#grid .book.sel').count() === 3 && (await p.textContent('#bulkbar b')) === '3 selected', 'three covers selected, bar says 3');
  ok(await p.evaluate(() => document.querySelector('#grid .book.sel').getAttribute('aria-pressed') === 'true'), 'selected card has aria-pressed=true');
  ok((await p.textContent('#bulkbar [data-bulk=trash]')) === 'Move to trash', 'the bar offers Move to trash (with Undo after, no second click)');
  await p.locator('#grid .book').nth(4).click(); ok((await p.textContent('#bulkbar b')) === '4 selected', 'a fourth cover: 4 selected');
  await p.locator('#grid .book').nth(4).click();
  const rowBtn = p.locator('#boards .rowsel').first(); await rowBtn.click();
  const cols = await p.evaluate(() => getComputedStyle(document.getElementById('grid')).gridTemplateColumns.split(' ').length);
  ok(await p.locator('#grid .book.sel').count() >= cols && (await rowBtn.getAttribute('aria-pressed')) === 'true', `row toggle selects the first row (${cols} columns)`);
  await p.click('#sel-none'); ok(await p.locator('#grid .book.sel').count() === 0 && !(await p.locator('#bulkbar').count()), 'None clears and the bar goes');
  await p.click('#sel-all'); ok(await p.locator('#grid .book.sel').count() === nBooks, 'All selects every shown book');
  await p.keyboard.press('Escape'); ok(!(await p.evaluate(() => st.selecting)) && await p.locator('#grid .book.sel').count() === 0, 'Esc leaves Select mode');
  // detail
  await p.locator('#grid .book').nth(5).click(); await p.waitForTimeout(1900);
  ok(await p.evaluate(() => !document.getElementById('detail').hidden && document.querySelector('#grid .book[aria-current=true]')?.dataset.id === st.selected), 'cover opens the detail; card marked current');
  await p.click('#d-close'); await p.waitForTimeout(600); ok(await p.evaluate(() => document.getElementById('detail').hidden), 'detail closes');
  // drag reorder (manual sort), persisted
  const ids0 = await p.evaluate(() => [...document.querySelectorAll('#grid .book')].slice(0, 4).map(b => b.dataset.id));
  const a = await p.locator('#grid .book').nth(0).boundingBox(), t = await p.locator('#grid .book').nth(3).boundingBox();
  await p.mouse.move(a.x + a.width / 2, a.y + 80); await p.mouse.down(); await p.mouse.move(a.x + 60, a.y + 90, {steps: 4}); await p.mouse.move(t.x + t.width / 2 - 10, t.y + 80, {steps: 8}); await p.mouse.up();
  await p.waitForTimeout(800);
  const ids1 = await p.evaluate(() => [...document.querySelectorAll('#grid .book')].slice(0, 4).map(b => b.dataset.id));
  const srv = await p.evaluate(async () => { const r = await (await fetch('api/library')).json(); return r.shelves.find(s => s.id === st.shelf).items.slice(0, 4); });
  ok(ids1[2] === ids0[0] && JSON.stringify(srv) === JSON.stringify(ids1), 'drag moves the first cover before the fourth, saved on the server');
  // chips and search
  const nRead = await p.evaluate(() => +document.querySelector('#f-completed .n').textContent);
  await p.click('#f-completed'); ok(await p.locator('#grid .book').count() === nRead, `Read chip shows ${nRead}`);
  await p.click('#f-all');
  await p.click('#filter-q'); await p.keyboard.type('the', {delay: 30}); await p.waitForTimeout(400);
  const fq = await p.evaluate(() => [document.activeElement.id, document.activeElement.selectionStart, document.querySelectorAll('#grid .book').length]);
  ok(fq[0] === 'filter-q' && fq[1] === 3 && fq[2] < nBooks && fq[2] > 0, 'filter box keeps focus and caret while the shelf filters: ' + JSON.stringify(fq));
  await p.fill('#filter-q', ''); await p.waitForTimeout(400);
  // list: windowed at 10x
  await p.evaluate(n => { const o = LIB.items.slice(); const out=[]; for (let k=0;k<n;k++) for (const i of o) out.push(k ? {...i, id: i.id+'_'+k} : i); LIB.items = out;
    const sh = LIB.shelves.find(s => s.id === st.shelf); sh.items = LIB.items.filter(i => i.kind === 'book').map(i => i.id); render(); }, 10);
  await p.click('#lay-list'); await p.waitForTimeout(300);
  const total = await p.evaluate(() => LWIN && LWIN.list.length), inDom = await p.locator('table.list tbody tr[data-id]').count();
  ok(total > 1000 && inDom < 200, `list is windowed: ${inDom} rows drawn of ${total}`);
  await p.evaluate(() => scrollTo(0, document.body.scrollHeight)); await p.waitForTimeout(300);
  const lastId = await p.evaluate(() => LWIN.list[LWIN.list.length - 1].id);
  ok(await p.locator(`table.list tr[data-id="${lastId}"]`).count() === 1, 'scrolled to the end: the last row is drawn');
  const tbH = await p.evaluate(() => { const tb = document.querySelector('table.list tbody'); return [tb.offsetHeight, LWIN.list.length * LWIN.rowH]; });
  ok(Math.abs(tbH[0] - tbH[1]) < 3, 'table height = rows x row height: ' + JSON.stringify(tbH));
  await p.click('table.list input[data-lselall]');
  ok((await p.textContent('#bulkbar b')) === total + ' selected', 'header check selects every row, drawn or not');
  await p.evaluate(() => scrollTo(0, 0)); await p.waitForTimeout(200);
  ok(await p.evaluate(() => [...document.querySelectorAll('table.list input[data-lsel]')].every(c => c.checked)), 'rows drawn later come checked');
  await p.click('table.list input[data-lselall]');
  await p.evaluate(() => render());       // back to the real library for Edit mode
  await p.evaluate(async () => { LIB = await api.library(); render(); }); await p.waitForTimeout(300);
  // edit mode: drafts survive a redraw; lazy language select takes a paste
  await p.click('#tbl-edit'); await p.waitForSelector('tr.editing');
  await p.locator('tr.editing').nth(0).locator('[data-f=title]').fill('Draft title kept');
  await p.evaluate(() => render());
  ok(await p.locator('tr.editing').nth(0).locator('[data-f=title]').inputValue() === 'Draft title kept' && await p.locator('tr.editing').nth(0).locator('td.dirty').count() === 1, 'a typed title survives a redraw (still marked changed)');
  await p.locator('tr.editing').nth(1).locator('[data-f=lang]').click({force: true}).catch(() => {});
  await p.keyboard.press('Escape').catch(() => {});
  await p.evaluate(() => { const tr = document.querySelectorAll('tr.editing')[2]; G.a = G.f = [2, GCOLS.indexOf('lang')]; gpaste('Italian'); });
  ok(await p.evaluate(() => document.querySelectorAll('tr.editing')[2].querySelector('[data-f=lang]').value) === 'ita', 'paste "Italian" into a not-yet-opened language list sets ita');
  ok(await p.evaluate(() => document.querySelectorAll('tr.editing [data-f=lang] option').length) < 1000, 'language lists stay small until opened (' + await p.evaluate(() => document.querySelectorAll('tr.editing [data-f=lang] option').length) + ' options in 154 rows)');
  await p.click('#tbl-cancel');
  ok(errs.length === 0, 'no page errors' + (errs.length ? ': ' + errs.join(' | ') : ''));
  console.log(fails ? fails + ' FAILED' : 'ALL PASS');
  await b.close();
})();
