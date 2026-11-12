// 1.1.0 (D54): the Random order (a new one per opening, steady while browsing, Shuffle) and the titles in other
// languages (shown in the language picked, found by the filter box and the search by any of them, listed and edited
// in the detail).
const { chromium } = require('playwright');
const BASE = process.env.ML_BASE || 'http://127.0.0.1:8199/', PW = process.env.ML_PW || 'pw-bench';
let fails = 0; const ok = (c, m) => { if (!c) fails++; console.log(c ? 'PASS' : 'FAIL', m); };
(async () => {
  const b = await chromium.launch();
  const ctx = await b.newContext({ extraHTTPHeaders: {"Tailscale-User-Login": process.env.ML_LOGIN || "me@x"}, viewport: {width: 1400, height: 900} });
  const p = await ctx.newPage(); const errs = []; p.on('pageerror', e => errs.push(e.message));
  const ready = () => p.waitForFunction(() => LIB.shelves.length && document.querySelector('#kinds [aria-current=true]'));
  await p.goto(BASE); await ready();
  await p.evaluate(async pw => { const r = await api.unlock(pw); SESSION.unlockedUntil = r.expiresAt; render(); }, PW);
  const order = () => p.evaluate(() => [...document.querySelectorAll('#grid > .book')].map(e => e.dataset.id).join(','));

  // ---- Random order
  await p.click('#kind-book'); await p.evaluate(() => { st.listSort = null; }); await p.click('#lay-grid');
  await p.selectOption('#sort', 'random'); await p.waitForSelector('#shuffle');
  const n = await p.locator('#grid > .book').count();
  const a = await order();
  ok(n >= 5, `the shelf shows ${n} books in a random order`);
  ok(await p.locator('#sortdir').count() === 0, 'Random: the sort direction gives way to Shuffle');
  await p.click('#f-all'); await p.click('#size-s'); await p.click('#size-m');
  ok(await order() === a, 'the order stays while you browse (status chip, cover size)');
  await p.locator('#grid > .book').first().click(); await p.waitForSelector('#detail:not([hidden])');
  ok(await order() === a, 'the order stays when a book opens');
  await p.click('#shuffle');
  const s1 = await order();
  ok(s1 !== a && s1.split(',').sort().join() === a.split(',').sort().join(), 'Shuffle: the same books in another order');
  await p.click('#kind-movie'); await p.click('#kind-book');
  const s2 = await order();
  ok(s2 !== s1, 'opening the kind again: another order');
  await p.reload(); await ready(); await p.click('#kind-book');
  const s3 = await order();
  ok(await p.inputValue('#sort') === 'random' && s3 !== s2, 'opening the page again: still Random, another order');
  await p.selectOption('#sort', 'title');
  ok(await p.locator('#sortdir').count() === 1, 'another sort brings the direction button back');

  // ---- titles in other languages (a movie gets an Italian and a Danish title through the API)
  await p.evaluate(async pw => { const r = await api.unlock(pw); SESSION.unlockedUntil = r.expiresAt; render(); }, PW);
  await p.click('#kind-movie');
  const m = await p.evaluate(() => { const i = LIB.items.find(x => x.kind === 'movie' && !x.coll && !x.section); return i && {id: i.id, title: i.title}; });
  if (!m) { console.log('SKIP titles [no movie in this database]'); }
  else {
    const IT = 'Titolo italiano di prova', DA = 'Dansk prøvetitel';
    const st0 = await p.evaluate(async ([id, it, da]) => { await api.patch(id, {titles: {ita: it, dan: da}}); LIB = await api.library(); render(); return LIB.items.find(x => x.id === id).titles; }, [m.id, IT, DA]);
    ok(st0.ita === IT && st0.dan === DA, 'the library sends the titles in other languages');
    await p.selectOption('#sort', 'title');
    await p.fill('#filter-q', 'titolo italiano'); await p.waitForTimeout(400);
    ok(await p.locator(`#grid > .book[data-id="${m.id}"]`).count() === 1, 'the filter box finds the movie by its Italian title (titles shown as saved)');
    await p.fill('#filter-q', ''); await p.waitForTimeout(300);
    await p.selectOption('#tlang', 'ita');
    ok(await p.locator(`#b-${m.id} .t`).textContent() === IT, 'Italian titles: the card shows the Italian title');
    await p.selectOption('#tlang', 'dan');
    if (process.env.SHOTS) {
      await p.screenshot({path: process.env.SHOTS + '/titles-desktop.png'});
      await p.setViewportSize({width: 390, height: 780}); await p.waitForTimeout(300);
      await p.screenshot({path: process.env.SHOTS + '/titles-phone.png'});
      await p.setViewportSize({width: 1400, height: 900}); await p.waitForTimeout(300);
    }
    const shownDa = await p.locator(`#b-${m.id} .t`).textContent();
    ok(shownDa === DA, `Danish titles: the card shows the Danish one (${JSON.stringify(shownDa)})`);
    const other = await p.evaluate(() => { const shown = new Set([...document.querySelectorAll('#grid > .book:not(.group)')].map(e => e.dataset.id));
      const i = LIB.items.find(x => shown.has(x.id) && !Object.keys(x.titles || {}).length && x.origLang !== 'dan'); return i && i.id; });
    if (other) ok(await p.locator(`#b-${other} .t`).textContent() === await p.evaluate(id => LIB.items.find(x => x.id === id).title, other), 'a movie with no Danish title keeps its own');
    await p.click('#lay-list');
    ok(await p.locator(`[data-ltitle="${m.id}"]`).innerText() === DA, 'the list shows the chosen language too');
    await p.click('#lay-grid');
    await p.reload(); await ready(); await p.click('#kind-movie');
    ok(await p.inputValue('#tlang') === 'dan' && await p.locator(`#b-${m.id} .t`).textContent() === DA, 'the choice is kept in this browser');
    await p.fill('#search', 'prøvetitel'); await p.waitForSelector('#search-pop:not([hidden]) .pop-item');
    ok((await p.locator('#search-pop .pop-name').first().innerText()).includes(DA), 'the search finds it by the Danish title');
    await p.fill('#search', 'titolo italiano'); await p.waitForTimeout(200);
    const hit = await p.locator('#search-pop .pop-name').first().innerText();
    ok(hit.includes(DA) && hit.includes(IT), `the search names the title it matched next to the one shown: "${hit}"`);
    await p.keyboard.press('Escape'); await p.fill('#search', '');
    await p.evaluate(id => openDetail(id), m.id); await p.waitForSelector('#d-title');
    ok(await p.locator('#d-title').innerText() === DA, 'the detail heading shows the chosen language');
    const row = await p.locator('dt:text-is("Titles") + dd').innerText();
    ok(row.includes(m.title) && row.includes(IT) && !row.includes(DA), `the detail lists the other titles: ${JSON.stringify(row)}`);
    await p.selectOption('#tlang', '');
    ok(await p.locator('#d-title').innerText() === m.title, 'back to Titles as saved: the heading follows');
    await p.click('#ed-edit'); await p.waitForSelector('#ef-t-ita');
    ok(await p.inputValue('#ef-t-ita') === IT && await p.inputValue('#ef-t-eng') === '', 'the editor has a field per language');
    await p.fill('#ef-t-eng', 'Test English title'); await p.fill('#ef-t-dan', '');
    await p.locator('#edform button[type=submit]').click();
    await p.waitForFunction(() => !document.querySelector('#edform'));
    const t2 = await p.evaluate(id => LIB.items.find(x => x.id === id).titles, m.id);
    ok(t2.eng === 'Test English title' && !t2.dan && t2.ita === IT, `saving the editor sets and removes titles: ${JSON.stringify(t2)}`);
  }
  ok(errs.length === 0, 'no page errors' + (errs.length ? ': ' + errs.join(' | ') : ''));
  console.log(fails ? fails + ' FAILED' : 'ALL PASS');
  await b.close();
})();
