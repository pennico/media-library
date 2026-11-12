// D72: the open item's info as a page (expand / reduce): its own address (#page=<id>), Back and Esc to the panel where
// you were (shelf and scroll), ‹ › on the page, a link straight into the page, close from the page, the phone.
const { chromium } = require('playwright');
const BASE = process.env.ML_BASE || 'http://127.0.0.1:8199/';
let fails = 0; const ok = (c, m) => { if (!c) fails++; console.log(c ? 'PASS' : 'FAIL', m); };
(async () => {
  const b = await chromium.launch();
  const ctx = await b.newContext({ extraHTTPHeaders: {"Tailscale-User-Login": process.env.ML_LOGIN || "me@x"}, viewport: {width: 1400, height: 900}, reducedMotion: 'reduce' });
  const p = await ctx.newPage(); const errs = []; p.on('pageerror', e => errs.push(e.message));
  const ready = () => p.waitForFunction(() => LIB.shelves.length && document.querySelector('#kinds [aria-current=true]'));
  await p.goto(BASE); await ready();
  await p.click('#kind-book'); await p.evaluate(() => { st.listSort = null; }); await p.click('#lay-grid'); await p.selectOption('#sort', 'title');
  const ids = await p.evaluate(() => [...document.querySelectorAll('#grid > .book')].map(e => e.dataset.id)), N = ids.length;
  const opened = id => p.waitForFunction(i => st.selected === i && st.detail && st.detail.id === i, id);
  const full = () => p.evaluate(() => document.getElementById('lib').classList.contains('fullpage'));
  await p.evaluate(() => scrollTo(0, 360)); await p.waitForTimeout(100);
  const y0 = await p.evaluate(() => Math.round(scrollY));
  const pick = await p.evaluate(() => { const r = [...document.querySelectorAll('#grid > .book')].find(e => { const b = e.getBoundingClientRect(); return b.top > 150 && b.bottom < innerHeight; }); return r.dataset.id; });
  const k = ids.indexOf(pick);
  await p.click('#b-' + pick); await opened(pick);
  const h0 = await p.evaluate(() => history.length);
  await p.click('#pd-full'); await p.waitForFunction(() => document.getElementById('lib').classList.contains('fullpage'));
  ok(await p.evaluate(i => location.hash === '#page=' + i, pick), 'the page has its own address');
  ok(await p.evaluate(() => history.length) === h0 + 1, 'a history entry of its own (Back returns to the panel)');
  const lay = await p.evaluate(() => { const d = document.getElementById('detail').getBoundingClientRect(), secs = [...document.querySelectorAll('#detail .dbody > .d-sec')].map(e => Math.round(e.getBoundingClientRect().left));
    return {w: Math.round(d.width), shelf: getComputedStyle(document.getElementById('content')).display, bar: getComputedStyle(document.getElementById('dbhead')).display, cols: new Set(secs).size, y: Math.round(scrollY), title: document.title}; });
  ok(lay.w > 900 && lay.shelf === 'none' && lay.bar === 'none', `the info takes the page, the shelf and its bar step aside ${JSON.stringify(lay)}`);
  ok(lay.cols === 2, 'its sections run in two columns on a wide screen');
  ok(lay.y === 0, 'the page starts at its top');
  const t0 = await p.evaluate(i => tt(LIB.items.find(x => x.id === i)), pick);
  ok(lay.title.startsWith(t0), 'the browser tab shows the item\'s title');
  ok((await p.getAttribute('#pd-copy', 'data-copy') || '').endsWith('#page=' + pick), 'Copy link copies the page\'s address');
  if (process.env.SHOTS) await p.screenshot({path: process.env.SHOTS + '/page-desktop.png', fullPage: false});
  // ‹ › on the page
  if (k < N - 1) {
    await p.click('#pn-next'); await opened(ids[k + 1]);
    ok(await full() && await p.evaluate(i => location.hash === '#page=' + i, ids[k + 1]) && await p.evaluate(() => history.length) === h0 + 1, 'Next on the page: the next item\'s page, the address replaced');
    ok((await p.textContent('.pn-pos')) === `${k + 2} of ${N}`, 'its place in the list');
  }
  const cur = await p.evaluate(() => st.selected);
  // reduce: back to the panel where you were
  await p.click('#pd-full'); await p.waitForFunction(() => !document.getElementById('lib').classList.contains('fullpage'));
  await p.waitForTimeout(150);
  const back = await p.evaluate(() => ({hash: location.hash, y: Math.round(scrollY), shelf: getComputedStyle(document.getElementById('content')).display, sel: st.selected}));
  ok(back.hash === '#item=' + cur && back.sel === cur && back.shelf !== 'none', `reduce: the panel with the same item, the shelf back ${JSON.stringify(back)}`);
  ok(Math.abs(back.y - y0) < 120, `the shelf is scrolled where it was (${y0} → ${back.y})`);
  ok(await p.evaluate(i => document.getElementById('b-' + i).getAttribute('aria-current') === 'true', cur), 'the shelf marks the item');
  ok(await p.evaluate(() => history.length) === h0 + 1, 'no extra history entries');
  // Back and Forward, Esc
  await p.click('#pd-full'); await p.waitForFunction(() => document.getElementById('lib').classList.contains('fullpage'));
  await p.goBack(); await p.waitForFunction(() => !document.getElementById('lib').classList.contains('fullpage'));
  ok(await p.evaluate(i => st.selected === i && location.hash === '#item=' + i, cur), 'Back: the panel');
  await p.goForward(); await p.waitForFunction(() => document.getElementById('lib').classList.contains('fullpage'));
  ok(await p.evaluate(i => st.selected === i, cur), 'Forward: the page again');
  await p.keyboard.press('Escape'); await p.waitForFunction(() => !document.getElementById('lib').classList.contains('fullpage'));
  ok(await p.evaluate(i => st.selected === i && location.hash === '#item=' + i, cur), 'Esc on the page: the panel');
  // close from the page: the console, scrolled where it was
  await p.click('#pd-full'); await p.waitForFunction(() => document.getElementById('lib').classList.contains('fullpage'));
  await p.click('#d-close'); await p.waitForTimeout(500);
  const shut = await p.evaluate(() => ({sel: st.selected, hash: location.hash, full: document.getElementById('lib').classList.contains('fullpage'), y: Math.round(scrollY), title: document.title}));
  ok(!shut.sel && shut.hash === '' && !shut.full && shut.title === 'Media Library', `✕ on the page closes it all ${JSON.stringify(shut)}`);
  // a link straight into the page
  const p2 = await ctx.newPage(); p2.on('pageerror', e => errs.push(e.message));
  await p2.goto(BASE + '#page=' + ids[2]);
  await p2.waitForFunction(i => st.selected === i && document.getElementById('lib').classList.contains('fullpage'), ids[2]);
  ok(true, 'a #page= link opens the console on that page');
  await p2.click('#pd-full'); await p2.waitForFunction(() => !document.getElementById('lib').classList.contains('fullpage'));
  ok(await p2.evaluate(i => location.hash === '#item=' + i && st.kind === 'book', ids[2]), 'reduce: its panel on its shelf');
  await p2.goBack(); await p2.waitForFunction(() => !st.selected);
  ok(await p2.evaluate(() => location.hash === ''), 'Back again: the console');
  await p2.close();
  // phone: a plain page, no sheet
  await p.setViewportSize({width: 375, height: 760}); await p.waitForTimeout(300);
  await p.evaluate(i => openDetail(i), ids[3]); await opened(ids[3]);
  await p.click('#pd-full'); await p.waitForFunction(() => document.getElementById('lib').classList.contains('fullpage'));
  const ph = await p.evaluate(() => { const d = document.getElementById('detail'); return {pos: getComputedStyle(d).position, scrim: document.getElementById('scrim').classList.contains('on'), sw: document.documentElement.scrollWidth, vw: innerWidth}; });
  ok(ph.pos === 'static' && !ph.scrim && ph.sw <= ph.vw + 1, `phone: a page, not the sheet, nothing sideways ${JSON.stringify(ph)}`);
  await p.evaluate(() => scrollTo(0, 900)); await p.waitForTimeout(150);
  const sticky = await p.evaluate(() => { const h = document.querySelector('header.top').getBoundingClientRect(), t = document.querySelector('#detail > .dtop').getBoundingClientRect(); return {gap: Math.round(t.top - h.bottom)}; });
  ok(Math.abs(sticky.gap) <= 2, `phone: ‹ › and reduce stay under the top bar ${JSON.stringify(sticky)}`);
  if (process.env.SHOTS) await p.screenshot({path: process.env.SHOTS + '/page-phone.png'});
  await p.click('#pd-full'); await p.waitForFunction(() => !document.getElementById('lib').classList.contains('fullpage'));
  ok(await p.evaluate(() => document.getElementById('scrim').classList.contains('on')), 'phone: reduce gives the sheet back');
  ok(errs.length === 0, 'no page errors' + (errs.length ? ': ' + errs.join(' | ') : ''));
  console.log(fails ? fails + ' FAILED' : 'ALL PASS');
  await b.close();
})();
