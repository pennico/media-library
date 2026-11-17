// D76-D80: book series. Create series… on a selection (the name asked with a suggestion, the order shown, a book in
// another series named), Undo; one card on the shelf (stacked covers, count, "2 of 4 read"); the series' panel (order,
// ⤒ ↑ ↓, remove, rename, delete, each with Undo); a book's strip and "Book 2 of 3"; Add to series…, Remove from series;
// ‹ › counting the card once; the page view; the list layout; a section; search; the trash; Movies' film collection;
// an older kept library; 375 px; locked.
const { chromium } = require('playwright');
const BASE = process.env.ML_BASE || 'http://127.0.0.1:8199/', PW = process.env.ML_PW || 'pw-bench';
let fails = 0; const ok = (c, m) => { if (!c) fails++; console.log(c ? 'PASS' : 'FAIL', m); };
(async () => {
  const b = await chromium.launch();
  const ctx = await b.newContext({ extraHTTPHeaders: {"Tailscale-User-Login": process.env.ML_LOGIN || "me@x"}, viewport: {width: 1400, height: 900} });
  const p = await ctx.newPage(); const errs = []; p.on('pageerror', e => errs.push(e.message));
  await p.goto(BASE); await p.waitForFunction(() => LIB.shelves.length && document.querySelector('#kinds [aria-current=true]'));
  await p.click('#kind-book'); await p.waitForSelector('#grid .book');
  if (await p.locator('#sort').inputValue() !== 'title') { await p.selectOption('#sort', 'title'); await p.waitForTimeout(200); }
  // four books of the main library with four different years, oldest first
  const [A, B, C, D] = await p.evaluate(() => { const seen = new Set(), out = [];
    for (const i of LIB.items.filter(i => i.kind === 'book' && !i.section && !i.coll && i.published).sort((a, b) => a.published - b.published))
      if (!seen.has(i.published)) { seen.add(i.published); out.push({id: i.id, title: tt(i), year: i.published}); }
    return [out[0], out[2], out[5], out[3]].sort((a, b) => a.year - b.year); });
  ok(A && B && C && D && A.year < B.year && B.year < C.year && C.year < D.year, `four dated books (${[A, B, C, D].map(x => x?.year).join(', ')})`);
  const sel = ids => p.evaluate(ids => { st.sel.clear(); toggleSel(ids, true); selChanged(); }, ids);
  ok(await p.locator('[data-bulk=gnew]').count() === 0, 'locked: no Create series');
  await p.evaluate(async pw => { const r = await api.unlock(pw); SESSION.unlockedUntil = r.expiresAt; render(); }, PW);
  const undo = async () => { await p.locator('#toasts .toast:last-child .tact').click(); await p.waitForTimeout(500); };
  const lastToast = () => p.locator('#toasts .toast:last-child').textContent();
  const cards = () => p.evaluate(() => [...document.querySelectorAll('#grid > .book')].map(e => e.dataset.id));
  const gidOf = id => p.evaluate(id => LIB.items.find(i => i.id === id)?.coll?.id || null, id);
  const order = gid => p.evaluate(g => groupItems(g).map(i => i.id).join(','), gid);
  const server = gid => p.evaluate(async g => (await api.library()).items.filter(i => i.coll?.id === g).sort((a, b) => a.coll.pos - b.coll.pos).map(i => i.id).join(','), gid);
  const ids = (...xs) => xs.map(x => x.id).join(',');
  const name = 'Check series ' + Math.random().toString(36).slice(2, 7);

  // ---- Create series… on a selection: the name asked (a suggestion), the order shown, then one card
  await sel([C.id, A.id, B.id]);
  await p.waitForSelector('[data-bulk=gnew]');
  ok((await p.locator('[data-bulk=gnew]').textContent()).trim() === 'Create series…', 'the bar has Create series…');
  ok(await p.locator('[data-bulk=gadd]').count() === 0, 'no Add to series… while there is no series');
  await p.click('[data-bulk=gnew]'); await p.waitForSelector('#gc-name');
  const sug = await p.inputValue('#gc-name');
  ok(sug.length > 0 && await p.evaluate(() => document.activeElement?.id === 'gc-name'), `the name is asked, with a suggestion to type over ("${sug}")`);
  ok(await p.evaluate(() => [...document.querySelectorAll('.gc-list li')].map(l => l.textContent).join('|')) === [A, B, C].map(x => `${x.title} (${x.year})`).join('|'),
     'the dialog lists them in the order they go in (first publication)');
  await p.click('#gc-cancel'); await p.waitForTimeout(150);
  ok(await p.locator('#gc-form').count() === 0 && await p.evaluate(() => st.sel.size) === 3, 'Cancel: nothing made, the selection kept');
  await p.click('[data-bulk=gnew]'); await p.fill('#gc-name', name); await p.press('#gc-name', 'Enter');
  await p.waitForFunction(id => LIB.items.find(i => i.id === id)?.coll, A.id);
  const g = await gidOf(A.id);
  ok(await order(g) === ids(A, B, C) && await server(g) === ids(A, B, C), 'created: A, B, C by first publication (and on the server)');
  let cs = await cards();
  ok(cs.includes('c:' + g) && !cs.includes(A.id) && !cs.includes(B.id) && !cs.includes(C.id), 'one card on the shelf in place of the three');
  ok(await p.locator(`[id="b-c:${g}"] .gmark`).textContent() === '3', 'the card has 3 on its badge');
  ok((await p.locator(`[id="b-c:${g}"] .s`).textContent()).includes('of 3 read'), `the card says how many are read ("${(await p.locator(`[id="b-c:${g}"] .s`).textContent()).trim()}")`);
  ok(await p.evaluate(() => st.sel.size) === 0 && (await lastToast()).includes(name) && (await lastToast()).includes('Undo'), 'the selection is cleared; the message names it, with Undo');
  await undo();
  cs = await cards();
  ok(!(await gidOf(A.id)) && cs.includes(A.id) && cs.includes(C.id) && await p.evaluate(g => !(LIB.groups || []).some(x => x.id === g), g), 'Undo: three cards again, no series');
  ok(await server(g) === '', 'Undo: none on the server either');
  await sel([A.id, B.id, C.id]); await p.click('[data-bulk=gnew]'); await p.fill('#gc-name', name); await p.click('#gc-ok');
  await p.waitForFunction(id => LIB.items.find(i => i.id === id)?.coll, A.id);
  const G = await gidOf(A.id);
  ok(await order(G) === ids(A, B, C), 'made again');

  // ---- the series' panel
  await p.click(`[id="b-c:${G}"]`); await p.waitForSelector('#g-items');
  ok((await p.locator('#detail .eyebrow').textContent()).trim() === 'Book series' && (await p.locator('#d-title').textContent()) === name, 'the panel: "Book series" and its name');
  ok(await p.evaluate(() => [...document.querySelectorAll('#g-items .collfilm')].map(e => e.dataset.open).join(',')) === ids(A, B, C), 'its books in order');
  ok(await p.evaluate(() => [...document.querySelectorAll('#g-items .ce-num')].map(e => e.textContent).join(',')) === '1,2,3', 'numbered 1, 2, 3');
  ok(/\d of 3 read/.test(await p.locator('#g-read').textContent()), 'and "n of 3 read"');
  await p.click(`#gr-up-${C.id}`); await p.waitForTimeout(400);
  ok(await order(G) === ids(A, C, B) && await server(G) === ids(A, C, B), 'Up one: A, C, B (and on the server)');
  ok(await p.evaluate(id => document.activeElement?.id === 'gr-up-' + id, C.id), 'the moved row\'s button keeps the focus');
  await p.click(`#gr-top-${B.id}`); await p.waitForTimeout(400);
  ok(await order(G) === ids(B, A, C), 'To the top: B, A, C');
  ok(await p.locator(`[id="b-c:${G}"]`).getAttribute('data-rep') === B.id, 'the card shows the first one\'s cover');
  await undo();
  ok(await order(G) === ids(A, C, B) && await server(G) === ids(A, C, B), 'Undo: A, C, B');
  await p.click(`#gr-down-${C.id}`); await p.waitForTimeout(400);
  ok(await order(G) === ids(A, B, C), 'Down one: A, B, C');
  ok(await p.locator(`#gr-top-${A.id}`).isDisabled() && await p.locator(`#gr-down-${C.id}`).isDisabled(), 'the first can\'t go up, the last can\'t go down');
  // ‹ › on the shelf count the card once
  const nav = await p.evaluate(g => navList().filter(x => x === 'c:' + g || groupItems(g).some(i => i.id === x)), G);
  ok(nav.join() === 'c:' + G, '‹ ›: the series is one step of the list');
  ok(await p.locator('.pn-pos').count() === 1, 'the panel has "n of N"');
  // a book of it: "Book 2 of 3" and the strip with the one before and after
  await p.click(`#cf-${B.id}`); await p.waitForSelector('#d-serline');
  ok((await p.locator('#d-serline').textContent()).startsWith('Book 2 of 3'), `a book's panel: "${(await p.locator('#d-serline').textContent()).trim()}"`);
  ok(await p.locator('.collstrip .cs-film').count() === 3 && await p.locator('.collstrip .cs-head .muted').textContent() === '2 of 3', 'the series strip, 2 of 3');
  ok(await p.locator(`[id="b-c:${G}"]`).getAttribute('aria-current') === 'true', 'the series\' card is marked while one of its books is open');
  ok((await p.locator('#detail dl.kv').textContent()).includes('Series'), 'the details name its Series');
  await p.click('#cs-next'); await p.waitForFunction(id => st.selected === id && document.querySelector('#d-serline'), C.id);
  ok((await p.locator('#d-serline').textContent()).startsWith('Book 3 of 3'), '› in the strip: the next book, 3 of 3');
  await p.click('#d-serline .linkbtn'); await p.waitForSelector('#g-items');
  ok(await p.evaluate(g => st.selected === 'c:' + g, G), 'the series\' name leads back to its panel');
  // the page view (D72)
  await p.click('#pd-full'); await p.waitForTimeout(process.env.SHOTS ? 1500 : 500);
  ok(await p.evaluate(g => st.full && decodeURIComponent(location.hash) === '#page=c:' + g, G) && await p.locator('#g-items .collfilm').count() === 3, 'the series as a page, #page=c:<id>');
  if (process.env.SHOTS) await p.screenshot({path: process.env.SHOTS + '/series-page.png'});
  await p.click('#pd-full'); await p.waitForTimeout(400);
  ok(await p.evaluate(() => !st.full), 'reduce: the panel again');

  // ---- remove one from the panel, Undo puts it back in its place
  await p.click(`#gr-rm-${B.id}`); await p.waitForTimeout(400);
  ok(await order(G) === ids(A, C) && (await cards()).includes(B.id), 'remove: A, C; B is its own card again');
  await undo();
  ok(await order(G) === ids(A, B, C) && await server(G) === ids(A, B, C), 'Undo: B back in second place');

  // ---- Add to series… (where its date belongs), Remove from series on a selection
  await p.click('#d-close'); await sel([D.id]);
  await p.click('[data-bulk=gadd]'); await p.waitForSelector(`[id="gadd-${G}"]`);
  await p.click(`[id="gadd-${G}"]`); await p.waitForTimeout(500);
  ok(await order(G) === ids(A, B, C, D) && await p.locator(`[id="b-c:${G}"] .gmark`).textContent() === '4', 'Add to series…: D joins, 4 on the badge');
  await sel([D.id, B.id]); await p.waitForSelector('[data-bulk=gout]');
  await p.click('[data-bulk=gout]'); await p.waitForTimeout(500);
  ok(await order(G) === ids(A, C) && await server(G) === ids(A, C), 'Remove from series on a selection: A, C left');
  await undo();
  ok(await order(G) === ids(A, B, C, D) && await server(G) === ids(A, B, C, D), 'Undo: B and D back where they were');

  // ---- a book in another series: the dialog says it moves; Undo puts it back
  await sel([D.id]); await p.click('[data-bulk=gnew]'); await p.waitForSelector('#gc-move');
  ok((await p.locator('#gc-move').textContent()).includes(name), 'the dialog says which series it leaves');
  await p.fill('#gc-name', name + ' two'); await p.click('#gc-ok'); await p.waitForTimeout(500);
  const G2 = await gidOf(D.id);
  ok(G2 && G2 !== G && await order(G) === ids(A, B, C) && (await lastToast()).includes('moved'), 'created: D moved to the new one, the message says so');
  await undo();
  ok(await order(G) === ids(A, B, C, D) && await p.evaluate(g => !(LIB.groups || []).some(x => x.id === g), G2), 'Undo: D back in the first, the second gone');

  // ---- the trash keeps a book's place
  await p.evaluate(async id => { await api.bulk({items: [id], action: 'trash'}); LIB = await api.library(); render(); }, B.id);
  ok(await order(G) === ids(A, C, D), 'in the trash: out of view');
  await p.evaluate(async id => { await api.bulk({items: [id], action: 'restore'}); LIB = await api.library(); render(); }, B.id);
  ok(await order(G) === ids(A, B, C, D), 'restored: back in its place');

  // ---- a section holding one of them: each place folds what it shows; the panel lists all, with where
  const sid = await p.evaluate(async () => { const r = await api.newSection('Check section ' + Math.random().toString(36).slice(2, 6)); LIB = await api.library(); return r.id; });
  await p.evaluate(async ([id, s]) => { await api.bulk({items: [id], action: 'section', section: s}); LIB = await api.library(); render(); }, [D.id, sid]);
  ok(await p.locator(`[id="b-c:${G}"] .gmark`).textContent() === '3', 'the main shelf folds the three in no section');
  await p.click(`[id="b-c:${G}"]`); await p.waitForSelector('#g-items');
  ok((await p.locator(`#gr-${D.id}`).textContent()).includes('in Check section'), 'the panel lists the fourth, "in" its section');
  await p.evaluate(async ([id, s]) => { await api.bulk({items: [id], action: 'section', section: null}); await api.deleteSection(s); LIB = await api.library(); render(); }, [D.id, sid]);
  await p.click('#d-close');

  // ---- the list layout: one row per book, the series in the title's tip
  await p.click('#lay-list'); await p.waitForSelector('table.list');
  ok(await p.locator(`#r-${A.id}`).count() === 1 && await p.locator(`#r-${C.id}`).count() === 1, 'the list: a row per book');
  ok((await p.locator(`#r-${A.id} .ltitle`).getAttribute('title')).includes(name), 'the title\'s tip names the series');
  await p.click('#lay-grid'); await p.waitForSelector('#grid .book');

  // ---- the filter box and the search find the books by the series' name
  await p.fill('#filter-q', name); await p.waitForTimeout(300);
  ok((await cards()).join() === 'c:' + G, 'the filter box: the series\' name shows its card');
  await p.fill('#filter-q', ''); await p.waitForTimeout(200);
  await p.fill('#search', name); await p.waitForSelector('#search-pop:not([hidden]) .pop-item');
  ok(await p.locator('#search-pop .pop-item').count() === 4 && (await p.locator('#search-pop .pop-sub').first().textContent()).includes(name), 'search: its four books, each naming the series');
  await p.press('#search', 'Enter'); await p.waitForTimeout(600);
  ok(await p.evaluate(ids => ids.includes(st.selected), [A.id, B.id, C.id, D.id]) && await p.locator(`[id="b-c:${G}"]`).getAttribute('aria-current') === 'true', 'a hit opens the book, its series\' card marked');
  await p.click('#d-close');

  // ---- rename, delete (Undo)
  await p.click(`[id="b-c:${G}"]`); await p.waitForSelector('#gf-name');
  await p.fill('#gf-name', name + ' renamed'); await p.press('#gf-name', 'Enter'); await p.waitForTimeout(400);
  ok((await p.locator(`[id="b-c:${G}"] .t`).textContent()) === name + ' renamed' && (await p.locator('#d-title').textContent()) === name + ' renamed', 'Rename: the card and the panel');
  await p.click('#gf-delete'); await p.waitForTimeout(500);
  cs = await cards();
  ok(!cs.includes('c:' + G) && cs.includes(A.id) && cs.includes(D.id) && await p.evaluate(() => !st.selected), 'Delete: the books are cards again, the panel closes');
  await undo();
  ok(await order(G) === ids(A, B, C, D) && await server(G) === ids(A, B, C, D), 'Undo: the series is back, in its order');

  // ---- a library kept from before (no groups list, no positions): no error, still one card
  await p.evaluate(() => { delete LIB.groups; for (const i of LIB.items) if (i.coll) delete i.coll.pos; LIBREV++; render(); });
  ok((await cards()).includes('c:' + G), 'an older kept library still folds the series');
  await p.evaluate(async () => { LIB = await api.library(); render(); });
  await p.reload(); await p.waitForSelector('#grid .book'); await p.waitForTimeout(800);
  ok((await cards()).includes('c:' + G), 'after a reload: one card');
  await p.evaluate(async pw => { const r = await api.unlock(pw); SESSION.unlockedUntil = r.expiresAt; render(); }, PW);

  // ---- 375 px: the panel and the dialog fit
  await p.setViewportSize({width: 375, height: 760}); await p.waitForTimeout(300);
  await p.click(`[id="b-c:${G}"]`); await p.waitForSelector('#g-items'); await p.waitForTimeout(process.env.SHOTS ? 2000 : 400);
  const ov = await p.evaluate(() => { const d = document.getElementById('detail'); return {sw: d.scrollWidth, cw: d.clientWidth, page: document.documentElement.scrollWidth, vw: innerWidth}; });
  ok(ov.sw <= ov.cw + 1 && ov.page <= ov.vw + 1, `phone: the series' panel fits 375 px ${JSON.stringify(ov)}`);
  if (process.env.SHOTS) await p.screenshot({path: process.env.SHOTS + '/series-phone.png'});
  await p.click('#d-close'); await p.waitForTimeout(300);
  await sel([A.id, B.id]); await p.click('[data-bulk=gnew]'); await p.waitForSelector('#gc-name'); await p.waitForTimeout(200);
  const pop = await p.evaluate(() => { const r = document.getElementById('bulkpop').getBoundingClientRect(); return {l: r.left, r: r.right, t: r.top, vw: innerWidth}; });
  ok(pop.l >= 0 && pop.r <= pop.vw && pop.t >= 0, `phone: the dialog is on screen ${JSON.stringify(pop)}`);
  if (process.env.SHOTS) await p.screenshot({path: process.env.SHOTS + '/series-create-phone.png'});
  await p.click('#gc-cancel'); await sel([]);
  await p.setViewportSize({width: 1400, height: 900}); await p.waitForTimeout(300);
  if (process.env.SHOTS) {
    await p.click(`[id="b-c:${G}"]`); await p.waitForSelector('#g-items'); await p.waitForTimeout(2200); await p.screenshot({path: process.env.SHOTS + '/series-desktop.png'});
    await p.click(`#cf-${B.id}`); await p.waitForSelector('#d-serline'); await p.waitForTimeout(1500); await p.screenshot({path: process.env.SHOTS + '/series-book.png'});
    await p.click('#d-close'); await p.waitForTimeout(300);
    await sel([A.id, B.id, D.id]); await p.click('[data-bulk=gnew]'); await p.waitForSelector('#gc-name'); await p.waitForTimeout(300);
    await p.screenshot({path: process.env.SHOTS + '/series-create.png'}); await p.click('#gc-cancel'); await sel([]); }

  // ---- Movies: the same makes a film collection
  await p.click('#kind-movie'); await p.waitForTimeout(300);
  const test = await p.evaluate(() => (LIB.sections || []).find(s => s.kind === 'test')?.id);
  await p.evaluate(s => { st.section = s; st.selected = null; render(); }, test); await p.waitForSelector('#grid .book');
  const films = await p.evaluate(() => LIB.items.filter(i => i.kind === 'movie' && i.section === st.section && !i.coll).slice(0, 2).map(i => i.id));
  await sel(films); await p.waitForSelector('[data-bulk=gnew]');
  ok((await p.locator('[data-bulk=gnew]').textContent()).trim() === 'Create film collection…', 'Movies: Create film collection…');
  await p.click('[data-bulk=gnew]'); await p.fill('#gc-name', name + ' films'); await p.press('#gc-name', 'Enter');
  await p.waitForFunction(id => LIB.items.find(i => i.id === id)?.coll, films[0]);
  const F = await gidOf(films[0]);
  ok((await cards()).includes('c:' + F), 'one card for the two films');
  await p.click(`[id="b-c:${F}"]`); await p.waitForSelector('#g-items');
  ok((await p.locator('#detail .eyebrow').textContent()).trim() === 'Film collection', 'its panel: "Film collection"');
  await p.click('#gf-delete'); await p.waitForTimeout(400);
  ok(!(await cards()).includes('c:' + F), 'deleted');

  // ---- locked: no editing in the panel
  await p.click('#kind-book'); await p.waitForTimeout(300);
  await p.evaluate(async () => { await api.lock?.(); SESSION.unlockedUntil = null; render(); });
  await p.click(`[id="b-c:${G}"]`); await p.waitForSelector('#g-items');
  ok(await p.locator('#g-items .ce-acts').count() === 0 && await p.locator('#gf-name').count() === 0, 'locked: the panel lists, nothing to change');
  ok(errs.length === 0, 'no page errors' + (errs.length ? ': ' + errs.join(' | ') : ''));
  console.log(fails ? fails + ' FAILED' : 'ALL PASS');
  await b.close();
})();
