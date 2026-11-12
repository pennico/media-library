// D70: a collection's content edited from its own view: add (search the library, click or Enter), remove, put in order
// (up, down, to the top), each with Undo; Remove from collection on a selection; rename and delete; locked = no editor.
const { chromium } = require('playwright');
const BASE = process.env.ML_BASE || 'http://127.0.0.1:8199/', PW = process.env.ML_PW || 'pw-bench';
let fails = 0; const ok = (c, m) => { if (!c) fails++; console.log(c ? 'PASS' : 'FAIL', m); };
(async () => {
  const b = await chromium.launch();
  const ctx = await b.newContext({ extraHTTPHeaders: {"Tailscale-User-Login": process.env.ML_LOGIN || "me@x"}, viewport: {width: 1400, height: 900} });
  const p = await ctx.newPage(); const errs = []; p.on('pageerror', e => errs.push(e.message));
  await p.goto(BASE); await p.waitForFunction(() => LIB.shelves.length && document.querySelector('#kinds [aria-current=true]'));
  await p.click('#kind-book');
  ok(await p.locator('#shelf-menu').count() === 0, 'locked: no Edit collection button');
  await p.evaluate(async pw => { const r = await api.unlock(pw); SESSION.unlockedUntil = r.expiresAt; render(); }, PW);
  const name = 'Check coll ' + Math.random().toString(36).slice(2, 7);
  const sid = await p.evaluate(async n => { const r = await api.newShelf(n, 'book'); LIB = await api.library(); render(); return r.id; }, name);
  await p.click(`#shelf-${sid}`);
  await p.waitForSelector('#coll-add-first');
  ok(true, 'an empty collection offers to add books');
  await p.click('#coll-add-first'); await p.waitForSelector('#ce-q');
  ok(await p.evaluate(() => document.activeElement?.id === 'ce-q'), 'the editor opens with the search box focused');
  const books = await p.evaluate(() => LIB.items.filter(i => i.kind === 'book' && !hiddenItem(i)).slice(0, 4).map(i => ({id: i.id, title: tt(i)})));
  const [A, B, C, D] = books;
  const order = () => p.evaluate(s => LIB.shelves.find(x => x.id === s).items.join(','), sid);
  const server = () => p.evaluate(async s => (await api.library()).shelves.find(x => x.id === s).items.join(','), sid);
  const grid = () => p.evaluate(() => [...document.querySelectorAll('#grid > .book')].map(e => e.dataset.id).join(','));
  const undo = async () => { await p.locator('#toasts .toast:last-child .tact').click(); await p.waitForTimeout(400); };
  const word = t => t.split(/\s+/).sort((x, y) => y.length - x.length)[0];
  // add by clicking
  await p.fill('#ce-q', A.title); await p.waitForSelector(`#ce-add-${A.id}`);
  await p.click(`#ce-add-${A.id}`); await p.waitForFunction(id => LIB.shelves.find(s => s.id === st.shelf).items.includes(id), A.id);
  ok(await grid() === A.id, 'Add: the book is on the collection\'s shelf at once');
  ok(await p.locator(`#ceh-${A.id} .ce-acts`).textContent() === 'In it', 'the search result now says In it');
  ok((await p.locator('#toasts .toast:last-child').textContent()).includes('Undo'), 'Add has Undo in its message');
  // add by Enter (the first result that is not in it)
  for (const x of [B, C]) { await p.fill('#ce-q', x.title); await p.waitForSelector(`#ce-add-${x.id}`); await p.press('#ce-q', 'Enter');
    await p.waitForFunction(id => LIB.shelves.find(s => s.id === st.shelf).items.includes(id), x.id); }
  ok(await order() === [A, B, C].map(x => x.id).join(','), 'Enter adds the first result: A, B, C in that order');
  ok(await p.evaluate(() => document.activeElement?.id === 'ce-q'), 'the search box keeps the focus while adding');
  // move down, Undo
  await p.click(`#ce-down-${A.id}`); await p.waitForTimeout(300);
  ok(await order() === [B, A, C].map(x => x.id).join(',') && await server() === await order(), 'Down one: B, A, C (and on the server)');
  ok(await grid() === await order(), 'the shelf (sorted by hand) follows the new order');
  await undo();
  ok(await order() === [A, B, C].map(x => x.id).join(',') && await server() === await order(), 'Undo: A, B, C again');
  // to the top, up
  await p.click(`#ce-top-${C.id}`); await p.waitForTimeout(300);
  ok(await order() === [C, A, B].map(x => x.id).join(','), 'To the top: C, A, B');
  await p.click(`#ce-up-${B.id}`); await p.waitForTimeout(300);
  ok(await order() === [C, B, A].map(x => x.id).join(','), 'Up one: C, B, A');
  ok(await p.evaluate(id => document.activeElement?.id === 'ce-up-' + id, B.id), 'the moved row\'s button keeps the focus');
  ok(await p.locator(`#ce-top-${C.id}`).isDisabled() && await p.locator(`#ce-down-${A.id}`).isDisabled(), 'the first row can\'t go up, the last can\'t go down');
  // remove, Undo puts it back where it was
  await p.click(`#ce-rm-${B.id}`); await p.waitForTimeout(300);
  ok(await order() === [C, A].map(x => x.id).join(',') && await grid() === await order(), 'Remove: C, A');
  await undo();
  ok(await order() === [C, B, A].map(x => x.id).join(',') && await server() === await order(), 'Undo the remove: B back in its place');
  // a selection on the shelf: Remove from collection, Undo
  await p.click('#d-close');
  await p.evaluate(id => { toggleSel([id], true); selChanged(); }, A.id);
  await p.waitForSelector('[data-bulk=uncoll]');
  await p.click('[data-bulk=uncoll]'); await p.waitForTimeout(400);
  ok(await order() === [C, B].map(x => x.id).join(','), 'Remove from collection on a selection');
  ok(await p.evaluate(() => st.sel.size) === 0, 'the selection is cleared');
  await undo();
  ok(await order() === [C, B, A].map(x => x.id).join(','), 'Undo brings it back last, where it was');
  // the editor in another sort: a hint, and Show it on the shelf
  await p.selectOption('#sort', 'title'); await p.click('#shelf-menu'); await p.waitForSelector('#ce-usemanual');
  await p.click('#ce-usemanual'); await p.waitForTimeout(300);
  ok(await p.inputValue('#sort') === 'manual' && await grid() === await order(), 'Show it on the shelf: the shelf sorts by hand again');
  // phone: the editor is the sheet, nothing sticks out sideways
  if (process.env.SHOTS) await p.screenshot({path: process.env.SHOTS + '/coll-desktop.png'});
  await p.setViewportSize({width: 375, height: 760}); await p.waitForTimeout(400);
  const ov = await p.evaluate(() => { const d = document.getElementById('detail'); return {sw: d.scrollWidth, cw: d.clientWidth, page: document.documentElement.scrollWidth, vw: innerWidth}; });
  ok(ov.sw <= ov.cw + 1 && ov.page <= ov.vw + 1, `phone: the editor fits 375 px ${JSON.stringify(ov)}`);
  if (process.env.SHOTS) await p.screenshot({path: process.env.SHOTS + '/coll-phone.png'});
  await p.setViewportSize({width: 1400, height: 900}); await p.waitForTimeout(300);
  // rename, then delete with a confirmation (no Undo for that one)
  await p.fill('#sf-name', name + ' x'); await p.press('#sf-name', 'Enter'); await p.waitForTimeout(400);
  ok((await p.locator(`#shelf-${sid}`).textContent()).includes(name + ' x'), 'Rename: the left pane has the new name');
  await p.click('#sf-delete'); await p.waitForSelector('#sf-delete-yes');
  ok(await p.evaluate(s => !!LIB.shelves.find(x => x.id === s), sid), 'Delete asks first');
  await p.click('#sf-delete-yes'); await p.waitForTimeout(500);
  ok(await p.evaluate(s => !LIB.shelves.find(x => x.id === s), sid) && await p.locator(`#shelf-${sid}`).count() === 0, 'Delete: the collection is gone');
  ok(errs.length === 0, 'no page errors' + (errs.length ? ': ' + errs.join(' | ') : ''));
  console.log(fails ? fails + ' FAILED' : 'ALL PASS');
  await b.close();
})();
