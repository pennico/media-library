const { chromium } = require('playwright');
const BASE = process.env.ML_BASE || 'http://127.0.0.1:8199/', PW = process.env.ML_PW || 'pw-bench';
let fails = 0; const ok = (c, m) => { if (!c) fails++; console.log(c ? 'PASS' : 'FAIL', m); };
(async () => {
  const b = await chromium.launch();
  const ctx = await b.newContext({ extraHTTPHeaders: {"Tailscale-User-Login": process.env.ML_LOGIN || "me@x"}, viewport:{width:1700,height:900}});
  const p = await ctx.newPage(); p.on('pageerror', e=>console.log('PAGEERR', e.message));
  await p.goto(BASE); await p.waitForSelector('#grid .book, table.list tbody tr');
  await p.evaluate(async pw => { const r = await api.unlock(pw); SESSION.unlockedUntil = r.expiresAt; render(); }, PW);
  await p.click('#kind-book'); if (await p.locator('#lay-list[aria-pressed=false]').count()) await p.click('#lay-list');
  await p.click('#tbl-edit'); await p.waitForSelector('tr.editing');
  const cell = (r, c) => p.locator(`tr.editing:nth-of-type(${r+1}) >> nth=0`).locator(c === 'cats' ? '[data-lecats]' : `[data-f=${c}]`);
  const val = (r, f) => p.evaluate(([r, f]) => { const tr = document.querySelectorAll('tr.editing')[r]; const el = tr.querySelector(`[data-f=${f}]`); return el.tagName==='SELECT' ? el.selectedOptions[0].textContent : el.value; }, [r, f]);
  const paste = (text) => p.evaluate(t => { const dt = new DataTransfer(); dt.setData('text/plain', t); const el = document.activeElement && document.activeElement !== document.body ? document.activeElement : document.querySelector('tr.editing [data-f=title]'); el.dispatchEvent(new ClipboardEvent('paste', {clipboardData: dt, bubbles: true, cancelable: true})); }, text);
  const toastText = () => p.evaluate(() => (document.querySelector('.toast')||{}).textContent);

  // 1. select a block by drag: status/rating columns, rows 0-2 ; then paste a 3x3 block from "Excel"
  await p.locator('tr.editing').nth(0).locator('[data-f=status]').click();   // anchor at row0 status
  await paste('Read\t7\t2024-03-05\nTo read\t\t2023\nAbandoned\t9.6\t');
  ok(await val(0,'status')==='Read' && await val(0,'rating')==='7' && await val(0,'ended')==='2024-03-05', 'paste block row 0');
  ok(await val(1,'status')==='To read' && await val(1,'rating')==='—' || await val(1,'rating')==='', 'paste block row 1 (empty rating)');
  ok(await val(2,'status')==='Abandoned' && await val(2,'rating')==='10', 'paste row 2 (9.6 -> 10)');
  ok(await p.locator('td.dirty').count() >= 7, 'dirty cells tinted: ' + await p.locator('td.dirty').count());
  // 2. invalid value
  await p.locator('tr.editing').nth(3).locator('[data-f=rating]').click();
  await paste('99');   // single value, single cell -> native paste path (no tab) => no grid handling
  await paste('abc\t99');
  ok(await p.locator('td.bad').count() === 2, 'invalid status & rating flagged (2 expected): ' + await p.locator('td.bad').count());
  // 3. undo reverts the last paste only
  await p.keyboard.press('Control+z');
  ok(await val(0,'status')==='Read', 'undo of last paste leaves earlier paste');
  // 4. drag-select rating col rows 0-2, fill down with Ctrl+D
  const r0 = await p.locator('tr.editing').nth(0).locator('[data-f=rating]').boundingBox(), r2 = await p.locator('tr.editing').nth(4).locator('[data-f=rating]').boundingBox();
  await p.mouse.move(r0.x+5, r0.y+5); await p.mouse.down(); await p.mouse.move(r2.x+5, r2.y+5, {steps: 6}); await p.mouse.up();
  ok(await p.locator('td.gsel').count() === 5, 'drag selected 5 cells: ' + await p.locator('td.gsel').count());
  await p.keyboard.press('Control+d');
  ok([0,1,2,3,4].every(async r => true) && await val(4,'rating')==='7', 'fill down copied row0 rating to row4');
  // 5. copy range as TSV
  const tsv = await p.evaluate(() => { const dt = new DataTransfer(); const ev = new ClipboardEvent('copy', {clipboardData: dt, bubbles: true, cancelable: true}); document.dispatchEvent(ev); return dt.getData('text/plain'); });
  ok(tsv === '7\n7\n7\n7\n7', 'copy gives TSV: ' + JSON.stringify(tsv));
  // 6. one value pasted over a selection fills it; Delete clears
  await paste('3'); ok(await val(3,'rating')==='3', 'single value fills selection');
  await p.keyboard.press('Delete'); ok(await val(3,'rating')==='' || await val(3,'rating')==='—', 'delete clears');
  await p.keyboard.press('Control+z'); ok(await val(3,'rating')==='3', 'undo of delete restores');
  // 7. Escape clears selection without leaving edit mode
  await p.keyboard.press('Escape'); ok(await p.locator('tr.editing').count() > 0 && await p.locator('td.gsel').count()===0, 'escape clears selection, stays in edit');
  // 8. save and verify on the server
  const before = await p.evaluate(() => LIB.items.map(i => i.id).length);
  const t = Date.now(); await p.click('#tbl-save'); await p.waitForFunction(() => !document.querySelector('tr.editing'), null, {timeout: 20000});
  console.log('saved in', Date.now()-t, 'ms');
  const srv = await p.evaluate(async () => { const r = await (await fetch('api/library')).json(); const rows = [...document.querySelectorAll('table.list tbody tr[data-id]')].slice(0,5).map(tr => r.items.find(i => i.id === tr.dataset.id)); return rows.map(i => [i.title, i.status, i.rating, (i.reads.at(-1)||[])[0]]); });
  ok(srv.every(r => r[2] === 3), 'saved on the server: the five ratings are 3');
  ok(srv[0][1] === 'completed' && srv[0][3] === '2024-03-05' && srv[1][1] === 'planned' && srv[2][1] === 'dropped', 'saved on the server: the pasted statuses and date');
  // 9. (1.0.1) Edit from the grid goes to the spreadsheet; a movie's runtime, original language and Where are cells that save
  await p.click('#kind-movie'); if (await p.locator('#lay-grid[aria-pressed=false]').count()) await p.click('#lay-grid');
  await p.waitForTimeout(300);
  if (!(await p.locator('#grid > .book').count())) console.log('SKIP no movie on the dev shelf');
  else {
    ok(await p.locator('#tbl-edit svg.tblico').count() === 1, 'Edit shows its spreadsheet picture on the grid too');
    await p.click('#tbl-edit'); await p.waitForSelector('tr.editing');
    ok(await p.evaluate(() => st.listMode && !!document.querySelector('table.list')), 'Edit from the grid switches to the list');
    const id = await p.evaluate(() => [...document.querySelectorAll('tr.editing')].map(tr => LIB.items.find(i => i.id === tr.dataset.id)).find(i => i?.kind === 'movie')?.id);
    const row = p.locator(`#r-${id}`);
    ok(await row.locator('input[data-f=pages]').count() === 1 && await row.locator('select[data-f=format] option[value=disc]').count() === 1, 'a movie row has a runtime box and a Where list');
    await row.locator('[data-f=pages]').fill('101');
    await row.locator('[data-f=lang]').focus(); await row.locator('[data-f=lang]').selectOption('ita');
    await row.locator('[data-f=format]').selectOption('disc');
    await row.locator('[data-f=ended]').fill('2020-05-01');                // a date and a place in one save: the date makes the watch
    await p.click('#tbl-save'); await p.waitForFunction(() => !document.querySelector('tr.editing'), null, {timeout: 20000});
    const m = await p.evaluate(async id => (await (await fetch('api/library')).json()).items.find(i => i.id === id), id);
    ok(m.runtime === 101 && m.origLang === 'ita' && m.reads.at(-1)[4] === 'disc', 'saved on the server: runtime, original language and where: ' + JSON.stringify([m.runtime, m.origLang, m.reads.at(-1)[4]]));
  }
  console.log(fails ? fails + ' FAILED' : 'ALL PASS');
  await b.close();
})();
