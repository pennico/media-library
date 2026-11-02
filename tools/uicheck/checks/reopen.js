// Reopening the console: the kind in the left pane, the shelf on the page and the filters all belong together.
const { chromium } = require('playwright');
const BASE = process.env.ML_BASE || 'http://127.0.0.1:8199/';
let fails = 0; const ok = (c, m) => { if (!c) fails++; console.log(c ? 'PASS' : 'FAIL', m); };
(async () => {
  const b = await chromium.launch();
  const ctx = await b.newContext({ extraHTTPHeaders: {"Tailscale-User-Login": process.env.ML_LOGIN || "me@x"}, viewport: {width: 1400, height: 900}});
  const p = await ctx.newPage(); const errs = []; p.on('pageerror', e => errs.push(e.message));
  const drawn = () => p.waitForFunction(() => LIB.shelves.length && document.querySelector('#kinds [aria-current=true]'));
  // what the page shows: the kind in the pane, the shelf's kind, the kind of the items in the main view
  const shown = () => p.evaluate(() => ({
    pane: document.querySelector('#kinds [aria-current=true]')?.dataset.kind,
    shelf: LIB.shelves.find(s => s.id === st.shelf)?.media_kind,
    items: [...new Set(visibleItems().map(i => i.kind))],
    current: document.querySelector('#shelves [aria-current=true]')?.dataset.shelf === st.shelf,
  }));
  const agree = (s, kind) => s.pane === kind && s.shelf === kind && s.current && s.items.every(k => k === kind);
  await p.goto(BASE); await drawn();

  // the reported case: a movie shelf was opened once, then Books; close and reopen
  await p.click('#kind-movie');
  const movieAll = await p.evaluate(() => allShelf('movie').id);
  await p.click('#shelf-' + movieAll);
  await p.click('#kind-book');
  await p.reload(); await drawn();
  let s = await shown(); ok(agree(s, 'book'), 'Books, closed and reopened: pane, shelf and items are all books ' + JSON.stringify(s));

  // an install that already has the two out of step in this browser mends itself
  await p.evaluate(id => { save('kind', 'book'); save('shelf', id); }, movieAll);
  await p.reload(); await drawn();
  s = await shown(); ok(agree(s, 'book'), 'a kept shelf of another kind gives way to the kept kind ' + JSON.stringify(s));

  // each kind reopens as itself
  await p.click('#kind-series'); await p.reload(); await drawn();
  s = await shown(); ok(agree(s, 'series'), 'Series reopens on series ' + JSON.stringify(s));
  await p.click('#kind-movie'); await p.reload(); await drawn();
  s = await shown(); ok(agree(s, 'movie'), 'Movies reopens on movies ' + JSON.stringify(s));

  // a collection of the kept kind is still where it reopens
  await p.click('#kind-book');
  const manual = await p.evaluate(() => LIB.shelves.find(x => x.kind === 'manual' && x.media_kind === 'book')?.id || null);
  if (manual) {
    await p.click('#shelf-' + manual); await p.reload(); await drawn();
    ok(await p.evaluate(id => st.shelf === id, manual), 'a book collection opened last is still open after reopening');
  } else console.log('PASS (no book collection in this database: skipped the collection case)');

  ok(errs.length === 0, 'no page errors' + (errs.length ? ': ' + errs.join(' | ') : ''));
  console.log(fails ? fails + ' FAILED' : 'ALL PASS');
  await b.close();
})();
