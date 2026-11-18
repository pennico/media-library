// D83-D86: series suggestions. Fill missing suggests a series (never applies it); a book's panel offers Create series
// (no others: at once; with others: a list to tick, kept through a redraw), Add to "<your series>" and Not this series,
// each with Undo; the bar's Suggest series looks the selection up and lists what was found by series (yours first),
// applied in the numbers' order; a source that fails stops the run with Try again; 375 px; Movies; locked.
// Its books are made up here; Open Library answers from tools/uicheck/recorded/ (MEDIALIB_HTTP_RECORDED: no network).
const { chromium } = require('playwright');
const BASE = process.env.ML_BASE || 'http://127.0.0.1:8199/', PW = process.env.ML_PW || 'pw-bench';
let fails = 0; const ok = (c, m) => { if (!c) fails++; console.log(c ? 'PASS' : 'FAIL', m); };
(async () => {
  const b = await chromium.launch();
  const ctx = await b.newContext({ extraHTTPHeaders: {"Tailscale-User-Login": process.env.ML_LOGIN || "me@x"}, viewport: {width: 1400, height: 900} });
  const p = await ctx.newPage(); const errs = []; p.on('pageerror', e => errs.push(e.message));
  await p.goto(BASE); await p.waitForFunction(() => LIB.shelves.length && document.querySelector('#kinds [aria-current=true]'));
  await p.click('#kind-book'); await p.waitForSelector('#grid .book');
  await p.evaluate(async pw => { const r = await api.unlock(pw); SESSION.unlockedUntil = r.expiresAt; render(); }, PW);
  const sel = ids => p.evaluate(ids => { st.sel.clear(); toggleSel(ids, true); selChanged(); }, ids);
  const undo = async () => { await p.locator('#toasts .toast:last-child .tact').click(); await p.waitForTimeout(600); };
  const lastToast = () => p.locator('#toasts .toast:last-child').textContent();
  const text = s => p.locator(s).first().textContent();
  const open = async id => { await p.evaluate(id => openDetail(id), id); await p.waitForFunction(id => st.detail?.id === id && document.getElementById('d-title'), id); await p.waitForTimeout(150); };
  const gidOf = id => p.evaluate(id => LIB.items.find(i => i.id === id)?.coll?.id || null, id);
  const order = gid => p.evaluate(g => groupItems(g).map(i => i.id).join(','), gid);
  const server = gid => p.evaluate(async g => (await api.library()).items.filter(i => i.coll?.id === g).sort((a, b) => a.coll.pos - b.coll.pos).map(i => i.id).join(','), gid);
  const mk = (title, author, year, isbn) => p.evaluate(async x => (await api.addManual({title: x.title, authors: x.author, published: String(x.year), isbn: x.isbn, status: 'planned'})).id,
                                                       {title, author, year, isbn});
  // made-up books: a cycle of four (numbered against their dates), a series you have with its middle book loose, one with its series in the title
  const dawn = await mk('Lantern Dawn', 'Uma Checker', 2010, '9780999990018'), noon = await mk('Lantern Noon', 'Uma Checker', 2005, '9780999990025');
  const dusk = await mk('Lantern Dusk', 'Uma Checker', 2012, '9780999990032'), night = await mk('Lantern Night', 'Uma Checker', 2014, null);
  const e1 = await mk('Ember One', 'Lea Checker', 2015, '9780999990216'), e2 = await mk('Ember Two', 'Lea Checker', 2001, '9780999990223');
  const e3 = await mk('Ember Three', 'Lea Checker', 2018, '9780999990230'), glass = await mk('Glass Harbor (Tide Saga, Book 2)', 'Ivo Checker', 2019, '9780999990315');
  const ember = await p.evaluate(async ids => { const r = await api.newGroup('book', 'Ember Songs', ids, false); LIB = await api.library(); render(); return r.id; }, [e1, e3]);
  await p.waitForTimeout(300);
  ok([dawn, noon, dusk, night, e1, e2, e3, glass, ember].every(Boolean), 'made-up books and the series "Ember Songs"');
  const ids = (...xs) => xs.join(',');

  // ---- Fill missing suggests, never applies; the panel: Not this series, Create series at once, each with Undo
  await open(glass); await p.click('#ed-fill'); await p.waitForSelector('#d-sersug', {timeout: 15000});
  ok((await lastToast()).includes('A series is suggested: “Tide Saga”') && !(await gidOf(glass)), `Fill missing: a series suggested, nothing applied ("${(await lastToast()).trim().slice(0, 90)}")`);
  ok((await text('#d-sersug p')).startsWith('Its title says book 2 of “Tide Saga”.'), `the panel says where from and the number ("${(await text('#d-sersug p')).trim()}")`);
  ok((await text('#ss-new')).trim() === 'Create series “Tide Saga”', 'no other books in it: Create series without a list');
  if (process.env.SHOTS) await p.screenshot({path: process.env.SHOTS + '/suggest-panel.png'});
  await p.click('#ss-no'); await p.waitForFunction(() => !document.getElementById('d-sersug'));
  ok((await lastToast()).includes('no longer suggested for this book') && (await lastToast()).includes('Undo'), 'Not this series: hidden, with Undo');
  await undo(); await p.waitForSelector('#d-sersug');
  ok(await p.locator('#d-sersug').count() === 1, 'Undo: the suggestion is back');
  await p.click('#ss-new'); await p.waitForSelector('#d-serline');
  const T = await gidOf(glass);
  ok(T && (await text('#d-serline')).includes('Book 1 of 1') && await p.locator('#d-sersug').count() === 0, 'Create series: made at once, "Book 1 of 1", the suggestion gone');
  await undo(); await p.waitForSelector('#d-sersug');
  ok(!(await gidOf(glass)) && await p.locator('#d-serline').count() === 0, 'Undo: no series, the suggestion back');
  await p.click('#d-close'); await p.waitForTimeout(200);

  // ---- Suggest series on a selection: looked up, listed by series (yours first), in the numbers' order
  await sel([dawn, noon, dusk, night, e2]); await p.waitForSelector('[data-bulk=gsug]');
  ok((await text('[data-bulk=gsug]')).trim() === 'Suggest series', 'the bar has Suggest series');
  await p.click('[data-bulk=gsug]'); await p.waitForSelector('#ss-progress');
  await p.waitForFunction(() => /Looked up 5 books/.test(document.getElementById('ss-progress')?.textContent || ''), null, {timeout: 20000});
  ok(await p.evaluate(() => st.sel.size === 0 && st.selected === '__series'), 'the review list opens in the panel, the selection cleared');
  const list = () => p.evaluate(() => [...document.querySelectorAll('#ss-list .ss-series')].map(s => ({key: s.dataset.sskey, h: s.querySelector('h3').firstChild.textContent.trim(), from: s.querySelector('.ss-from')?.textContent,
    ids: [...s.querySelectorAll('[data-ssbook]')].map(c => c.dataset.ssbook).join(','), on: [...s.querySelectorAll('[data-ssbook]')].map(c => c.checked).join(','),
    btn: s.querySelector('[data-ssapply]').textContent, one: !!s.closest('details')})));
  let L = await list();
  if (process.env.SHOTS) await p.screenshot({path: process.env.SHOTS + '/suggest-review.png'});
  ok(L[0]?.h === 'Ember Songs' && L[0].btn === 'Add to “Ember Songs”' && L[0].ids === e2, 'a series you have comes first, with Add to');
  const lan = L.find(x => x.h === 'The Lantern Cycle');
  ok(lan && lan.ids === ids(dawn, noon, dusk, night) && lan.btn === 'Create series of 4' && lan.on === 'true,true,true,true', 'the cycle: its four books by their numbers (not their dates), ticked');
  ok(L.some(x => x.h === 'Tide Saga' && x.one && x.ids === glass && x.from === '· titles'), 'a series with one of your books: under its own heading, "from titles"');
  ok(lan.from === '· Open Library', 'where a series comes from, by its name');
  const S = `.ss-series[data-sskey="${lan.key}"]`;
  await p.uncheck(`${S} [data-ssbook="${night}"]`);
  await p.click(`${S} [data-ssapply]`); await p.waitForFunction(id => LIB.items.find(i => i.id === id)?.coll, dawn);
  const C = await gidOf(dawn);
  ok(await order(C) === ids(dawn, noon, dusk) && await server(C) === ids(dawn, noon, dusk), 'Create series: the ticked three, 1, 2, 3 by number (and on the server)');
  ok((await lastToast()).includes('“The Lantern Cycle” created with 3 books') && (await lastToast()).includes('Undo'), 'its message, with Undo');
  await undo(); await p.waitForFunction(id => !LIB.items.find(i => i.id === id)?.coll, dawn);
  L = await list();
  ok(L.find(x => x.key === lan.key)?.on === 'true,true,true,false', 'Undo: the four listed again, the untick kept');
  await p.click(`.ss-series[data-sskey="${L[0].key}"] [data-ssapply]`);
  await p.waitForFunction(([id, g]) => LIB.items.find(i => i.id === id)?.coll?.id === g, [e2, ember]);
  ok(await order(ember) === ids(e1, e2, e3) && await server(ember) === ids(e1, e2, e3), 'Add to “Ember Songs”: between 1 and 3 by its number (its date is the oldest)');
  await undo(); await p.waitForFunction(id => !LIB.items.find(i => i.id === id)?.coll, e2);
  ok(await order(ember) === ids(e1, e3), 'Undo: out again');
  await p.click(`.ss-series[data-sskey="${L[0].key}"] [data-ssno]`); await p.waitForTimeout(500);
  ok(!(await list()).some(x => x.key === L[0].key) && (await lastToast()).includes('no longer suggested'), 'Not a series: gone from the list');
  await undo(); await p.waitForTimeout(300);
  ok((await list()).some(x => x.key === L[0].key), 'Undo: listed again');
  await p.click('#d-close'); await p.waitForTimeout(200);

  // ---- a book's panel: Add to your series, by its number
  await open(e2); await p.waitForSelector('#d-sersug');
  ok((await text('#d-sersug p')).startsWith('Open Library has it as book 2 of “Ember Songs”.') && (await text('#ss-add')).trim() === 'Add to “Ember Songs”', 'Ember Two: Add to “Ember Songs”');
  await p.click('#ss-add'); await p.waitForSelector('#d-serline');
  ok((await text('#d-serline')).startsWith('Book 2 of 3') && await order(ember) === ids(e1, e2, e3) && await p.locator('#d-sersug').count() === 0, 'added: "Book 2 of 3", no suggestion');

  // ---- a book's panel: Create series with the others, ticked in a list (on a phone too)
  await open(noon); await p.waitForSelector('#d-sersug');
  ok((await text('#d-sersug p')).trim() === 'Open Library has it as book 2 of “The Lantern Cycle”. 3 more of your books are in it.', 'Lantern Noon: book 2, three more of yours');
  await p.click('#ss-new'); await p.waitForSelector('#sc-form');
  const rows = () => p.evaluate(() => [...document.querySelectorAll('#sc-list [data-scbook]')].map(c => c.dataset.scbook + (c.checked ? '+' : '-') + (c.disabled ? '!' : '')).join(','));
  ok(await rows() === `${dawn}+,${noon}+!,${dusk}+,${night}+`, 'the list: all four by number, this one fixed');
  ok(await p.inputValue('#sc-name') === 'The Lantern Cycle' && await p.evaluate(() => document.activeElement?.id === 'sc-name'), 'the name to type over');
  await p.uncheck(`#sc-list [data-scbook="${dusk}"]`); await p.fill('#sc-name', 'Lantern');
  await p.evaluate(() => renderDetail()); await p.waitForTimeout(100);
  ok(await p.inputValue('#sc-name') === 'Lantern' && await rows() === `${dawn}+,${noon}+!,${dusk}-,${night}+`, 'a redraw keeps the name and the ticks');
  await p.setViewportSize({width: 375, height: 760}); await p.waitForTimeout(500);
  const fit = await p.evaluate(() => { const f = document.getElementById('sc-form').getBoundingClientRect(), o = document.getElementById('sc-ok').getBoundingClientRect();
    return {l: f.left, r: f.right, ok: o.right, vw: innerWidth, over: document.documentElement.scrollWidth - innerWidth}; });
  ok(fit.l >= 0 && fit.r <= fit.vw && fit.ok <= fit.vw && fit.over <= 0, `phone: the form fits the screen ${JSON.stringify(fit)}`);
  if (process.env.SHOTS) await p.screenshot({path: process.env.SHOTS + '/suggest-form-phone.png'});
  await p.setViewportSize({width: 1400, height: 900}); await p.waitForTimeout(400);
  if (process.env.SHOTS) await p.screenshot({path: process.env.SHOTS + '/suggest-form.png'});
  await p.click('#sc-ok'); await p.waitForSelector('#d-serline');
  const N = await gidOf(noon);
  ok((await text('#d-serline')).startsWith('Book 2 of 3') && await order(N) === ids(dawn, noon, night), 'created "Lantern": 1, 2, 4 by number, "Book 2 of 3"');
  await open(dusk); await p.waitForSelector('#ss-add');
  ok((await text('#d-sersug p')).includes('book 3 of “The Lantern Cycle” (your series “Lantern”)'), 'Lantern Dusk: the name matches your series written otherwise');
  await p.click('#ss-add'); await p.waitForSelector('#d-serline');
  ok(await order(N) === ids(dawn, noon, dusk, night) && await server(N) === ids(dawn, noon, dusk, night), 'Add to “Lantern”: in its place, 3');

  // ---- a source that fails stops the run (here: a lookup with no recorded answer), Try again; 375 px
  const lone = await mk('Quiet Unknown', 'Zed Checker', 2020, null);
  await p.click('#d-close'); await p.evaluate(async () => { LIB = await api.library(); render(); });
  await sel([lone, glass]); await p.click('[data-bulk=gsug]'); await p.waitForSelector('#ss-retry', {timeout: 15000});
  const prog = await text('#ss-progress');
  ok(prog.includes('not among the recorded answers') && prog.includes('2 books not looked up') && (await text('#ss-retry')).trim() === 'Try again', `the run stops with why and Try again ("${prog.trim().slice(0, 50)}…")`);
  ok(await p.locator('#d-sersug').count() === 0 && (await list()).some(x => x.h === 'Tide Saga'), 'what was found before is still listed');
  await p.setViewportSize({width: 375, height: 760}); await p.waitForTimeout(500);
  const fit2 = await p.evaluate(() => { const d = document.getElementById('detail'), r = d.getBoundingClientRect();
    return {l: r.left, r: r.right, vw: innerWidth, inner: d.scrollWidth - d.clientWidth, over: document.documentElement.scrollWidth - innerWidth}; });
  ok(fit2.l >= 0 && fit2.r <= fit2.vw + 1 && fit2.inner <= 0 && fit2.over <= 0, `phone: the review list fits ${JSON.stringify(fit2)}`);
  if (process.env.SHOTS) await p.screenshot({path: process.env.SHOTS + '/suggest-review-phone.png'});
  await p.setViewportSize({width: 1400, height: 900}); await p.waitForTimeout(300);
  await p.click('#d-close'); await p.waitForTimeout(200);

  // ---- Movies: no Suggest series; locked: no suggestion in a panel
  await p.click('#kind-movie'); await p.waitForTimeout(300);
  const film = await p.evaluate(() => LIB.items.find(i => i.kind === 'movie' && !i.section)?.id);
  if (film) { await sel([film]); await p.waitForSelector('#bulkbar'); ok(await p.locator('[data-bulk=gsug]').count() === 0, 'Movies: no Suggest series'); await sel([]); }
  await p.click('#kind-book'); await p.waitForTimeout(300);
  await p.evaluate(async () => { await api.lock?.(); SESSION.unlockedUntil = null; render(); });
  await open(glass);
  ok(await p.locator('#d-sersug').count() === 0, 'locked: no suggestion in the panel');
  ok(errs.length === 0, 'no page errors' + (errs.length ? ': ' + errs.join(' | ') : ''));
  console.log(fails ? fails + ' FAILED' : 'ALL PASS');
  await b.close();
})();
