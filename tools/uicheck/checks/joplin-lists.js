// D73: the Sync page's Joplin lists: the lines left in the notes (why, candidates), what was added (a link to it), a
// failed client run, a conflict copy, and the phone. The page's data is the server's /api/reading-apps with a lists
// part made up here (the hourly job does not run in a browser check).
const { chromium } = require('playwright');
const BASE = process.env.ML_BASE || 'http://127.0.0.1:8199/';
let fails = 0; const ok = (c, m) => { if (!c) fails++; console.log(c ? 'PASS' : 'FAIL', m); };
(async () => {
  const b = await chromium.launch();
  const ctx = await b.newContext({ extraHTTPHeaders: {"Tailscale-User-Login": process.env.ML_LOGIN || "me@x"}, viewport: {width: 1400, height: 900} });
  const p = await ctx.newPage(); const errs = []; p.on('pageerror', e => errs.push(e.message));
  await p.goto(BASE); await p.waitForFunction(() => LIB.shelves.length && document.querySelector('#kinds [aria-current=true]'));
  const item = await p.evaluate(() => { const i = LIB.items.find(x => x.kind === 'book' && !hiddenItem(x)); return {id: i.id, title: i.title}; });
  let failed = false;
  await p.route(/\/api\/reading-apps$/, async route => {
    const res = await route.fetch(); const j = await res.json();
    const now = new Date().toISOString();
    j.lists = {
      lastRun: failed ? {at: now, status: 'failed', summary: {notes: [], added: [], taken: [], unclear: [], errors: [], client_error: "joplin-lists: syncing would raise the apps' minimum version from 3.6.0 to 3.7.0"}}
        : {at: now, status: 'ok', summary: {notes: [{id: 'a'.repeat(32), title: 'To watch', notebook: 'Lists', lines: 3}, {id: 'b'.repeat(32), title: 'To read', notebook: 'Lists', lines: 2},
          {id: 'c'.repeat(32), title: 'To watch', notebook: 'Lists', lines: 1, conflictCopy: true}], lines: 6,
          added: ['To read: Check Book → from apple: Check Book'], taken: ['To watch: Seen (2001) → already in the library (done)'], unclear: [], errors: []}},
      lines: [{note: 'a'.repeat(32), line: 'Dune', list: 'watch', state: 'unclear', problem: 'several fit: Dune (film, 2021), Dune (film, 1984); add the year or (film) / (series)',
               candidates: [{title: 'Dune', year: 2021, kind: 'movie', tmdb: 1}, {title: 'Dune', year: 1984, kind: 'movie', tmdb: 2}], since: now},
              {note: 'b'.repeat(32), line: '- A very long book title that nobody has ever written down anywhere at all, in any language', list: 'read', state: 'unclear',
               problem: 'no confident match at Apple Books / Open Library: add the author (Title - Author), or add the book in the console (the line then finds it)', candidates: [], since: now},
              {note: 'a'.repeat(32), line: 'Arrival', list: 'watch', state: 'taken', how: 'from TMDB', since: now, item: null}],
      added: [{line: 'Check Book', list: 'read', id: item.id, title: item.title, kind: 'book', at: now}],
    };
    await route.fulfill({response: res, json: j});
  });
  await p.click('#sec-sync'); await p.waitForSelector('#sync-lists');
  ok(await p.waitForFunction(() => document.querySelector('#sync-lists .n')?.textContent.trim() === '2', null, {timeout: 5000}).then(() => true, () => false),
    'the side pane counts the 2 lines left in the notes');
  await p.click('#sync-lists'); await p.waitForSelector('#v-sync h2');
  const txt = await p.textContent('#v-sync');
  ok(txt.includes('Left in the notes') && txt.includes('several fit') && txt.includes('Close: Dune (film, 2021) · Dune (film, 1984)'), 'a line left in To watch, with why and the candidates');
  ok(txt.includes('1 line taken but not deleted'), 'a line taken but still in the note is counted');
  ok(txt.includes('A conflict copy in Joplin') && txt.includes('Conflicts'), 'a conflict copy says to delete it in Joplin');
  ok(txt.includes('Recently added from the lists'), 'what the lists added');
  if (process.env.SHOTS) await p.screenshot({path: process.env.SHOTS + '/lists-desktop.png', fullPage: true});
  await p.setViewportSize({width: 375, height: 760}); await p.waitForTimeout(300);
  const ov = await p.evaluate(() => ({page: document.documentElement.scrollWidth, vw: innerWidth}));
  ok(ov.page <= ov.vw + 1, `phone: nothing sideways ${JSON.stringify(ov)}`);
  if (process.env.SHOTS) await p.screenshot({path: process.env.SHOTS + '/lists-phone.png', fullPage: true});
  await p.setViewportSize({width: 1400, height: 900}); await p.waitForTimeout(200);
  await p.click(`[data-gobook="${item.id}"]`); await p.waitForFunction(id => st.sec === 'database' && st.selected === id, item.id);
  ok(true, 'the added item opens in the Database');
  // a failed run: the card says what the client said
  failed = true;
  await p.click('#sec-sync'); await p.click('#sync-lists'); await p.waitForFunction(() => document.querySelector('#v-sync')?.textContent.includes('The Joplin client stopped'));
  ok((await p.textContent('#v-sync')).includes("minimum version from 3.6.0 to 3.7.0"), 'a failed run shows what the client said');
  ok(errs.length === 0, 'no page errors' + (errs.length ? ': ' + errs.join(' | ') : ''));
  console.log(fails ? fails + ' FAILED' : 'ALL PASS');
  await b.close();
})();
