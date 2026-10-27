const { chromium } = require('playwright');
const BASE = process.env.ML_BASE || 'http://127.0.0.1:8199/', PW = process.env.ML_PW || 'pw-bench';
let fails = 0; const ok = (c, m) => { if (!c) fails++; console.log(c ? 'PASS' : 'FAIL', m); };
(async () => {
  const b = await chromium.launch();
  const ctx = await b.newContext({ extraHTTPHeaders: {"Tailscale-User-Login": process.env.ML_LOGIN || "me@x"}, viewport:{width:1400,height:900}});
  const p = await ctx.newPage(); const errs = []; p.on('pageerror', e => errs.push(e.message));
  let posts = 0; p.on('request', r => { if (r.url().includes('/readings') && r.method() === 'POST') posts++; });
  await p.goto(BASE); await p.waitForSelector('#grid .book, table.list tbody tr');
  await p.evaluate(async pw => { const r = await api.unlock(pw); SESSION.unlockedUntil = r.expiresAt; render(); }, PW);
  await p.click('#kind-book'); if (await p.locator('#lay-grid[aria-pressed=false]').count()) await p.click('#lay-grid');
  await p.waitForSelector('#grid .book');
  // Escape in the filter box keeps a selection
  await p.click('#sel-toggle'); await p.locator('#grid .book').nth(1).click(); await p.locator('#grid .book').nth(2).click();
  await p.click('#filter-q'); await p.keyboard.press('Escape');
  ok(await p.evaluate(() => st.selecting && st.sel.size === 2), 'Escape in the filter box keeps Select mode and the 2 selected');
  await p.locator('#grid .book').nth(0).focus(); await p.keyboard.press('Escape');
  ok(await p.evaluate(() => !st.selecting), 'Escape on the shelf then leaves Select mode');
  // arrow keys
  await p.locator('#grid .book').nth(0).focus(); await p.keyboard.press('ArrowRight'); await p.keyboard.press('ArrowDown');
  const cols = await p.evaluate(() => gridRows(document.getElementById('grid')).cols);
  ok(await p.evaluate(c => [...document.querySelectorAll('#grid > .book')].indexOf(document.activeElement) === 1 + c, cols), 'arrow keys move right one and down a row');
  // count and clear
  await p.click('#f-completed'); await p.click('#filter-q'); await p.keyboard.type('zzzz-nothing'); await p.waitForTimeout(350);
  ok((await p.textContent('#v-database h2')).includes('0 of'), 'title shows "0 of N" when narrowed');
  await p.click('.empty [data-fclear]'); await p.waitForTimeout(100);
  ok(await p.evaluate(() => st.q === '' && st.filter === 'all' && document.querySelectorAll('#grid .book').length > 100), 'empty state "Clear the filters" also clears the text and the chip');
  // broken cover falls back to its title
  await p.evaluate(() => { const img = document.querySelector('#grid .book:nth-child(3) .cov img'); img.srcset = 'thumbs/' + 'e'.repeat(64) + '-480.jpg 480w'; });
  await p.waitForTimeout(600);
  ok(await p.evaluate(() => { const im = document.querySelector('#grid .book:nth-child(3) .cov img'); return im && !im.hasAttribute('srcset') && im.complete && im.naturalWidth > 0; }), 'a cover size that fails falls back to the 320 px one');
  await p.evaluate(() => { const img = document.querySelector('#grid .book:nth-child(4) .cov img'); img.removeAttribute('srcset'); img.src = 'thumbs/' + 'f'.repeat(64) + '-320.jpg'; });
  await p.waitForTimeout(500);
  ok(await p.evaluate(() => { const c = document.querySelector('#grid .book:nth-child(4) .cov'); return !c.querySelector('img') && c.querySelector('.ph')?.textContent.length > 0; }), 'a cover that fails to load shows the title');
  // detail: second book with the panel open: no flight; close returns focus
  await p.locator('#grid .book').nth(5).click(); await p.waitForTimeout(1900);
  await p.locator('#grid .book').nth(6).click(); await p.waitForTimeout(150);
  ok(await p.evaluate(() => !document.querySelector('.flybook') && !document.body.classList.contains('fb-pane')), 'next book with the panel open: no new flight');
  const sid = await p.evaluate(() => st.selected);
  await p.locator('#d-close').focus(); await p.keyboard.press('Enter'); await p.waitForTimeout(500);
  ok(await p.evaluate(id => document.activeElement?.id === 'b-' + id, sid), 'closing the panel puts focus back on its book');
  // double submit: two quick saves of a new reading make one
  await p.locator('#grid .book').nth(7).click(); await p.waitForTimeout(1900);
  const id7 = await p.evaluate(() => st.selected);
  posts = 0;
  await p.evaluate(async id => { const f = () => act(async () => { await api.addReading(id, {state: 'completed', ended: '2021'}); await afterItemChange(); }, 'Reading saved', 'reading:' + id + ':new'); await Promise.all([f(), f()]); }, id7);
  await p.waitForTimeout(400);
  ok(posts === 1, 'two quick submits of the same action send one request (' + posts + ')');
  // the lock: Save refused, kept, and done after unlocking
  await p.click('#d-close'); await p.click('#lay-list'); await p.click('#tbl-edit'); await p.waitForSelector('tr.editing');
  const r0 = p.locator('tr.editing').nth(0).locator('[data-f=pages]'); await r0.fill('321');
  await p.keyboard.press('Escape');
  ok(await p.evaluate(() => st.tableEdit && document.querySelectorAll('td.dirty').length === 1), 'Escape in a cell keeps the edit mode and the typed value');
  await r0.focus(); await p.keyboard.press('Enter');
  ok(await p.evaluate(() => document.activeElement === document.querySelectorAll('tr.editing')[1].querySelector('[data-f=pages]')), 'Enter moves to the same column in the next row');
  await p.evaluate(() => { SESSION.unlockedUntil = null; renderKeybar(); });       // the lock comes
  await p.click('#tbl-save'); await p.waitForTimeout(300);
  ok(await p.evaluate(() => !!st.pending && st.tableEdit && document.querySelectorAll('td.dirty').length === 1), 'Save while locked: nothing claimed, the edit is kept, the save waits');
  ok(!(await p.evaluate(() => lastToast())).startsWith('Saved'), 'no "Saved" toast while locked');
  await p.fill('#key-input', PW); await p.keyboard.press('Enter'); await p.waitForTimeout(1200);
  const saved = await p.evaluate(async () => { const r = await (await fetch('api/library')).json(); return r.items.find(i => i.id === document.querySelector('table.list tr[data-id]').dataset.id)?.pages; });
  ok(saved === 321 && !(await p.evaluate(() => st.tableEdit)), 'after unlocking the save runs by itself (pages = ' + saved + ')');
  ok(errs.length === 0, 'no page errors' + (errs.length ? ': ' + errs.join(' | ') : ''));
  console.log(fails ? fails + ' FAILED' : 'ALL PASS');
  await b.close();
})();
