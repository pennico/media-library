// The top bar stays on screen while the page scrolls (desktop, and phone width with the detail sheet), and a film of
// a collection switches to the collection's other films from its own detail.
const { chromium } = require('playwright');
const BASE = process.env.ML_BASE || 'http://127.0.0.1:8199/';
let fails = 0; const ok = (c, m) => { if (!c) fails++; console.log(c ? 'PASS' : 'FAIL', m); };
(async () => {
  const b = await chromium.launch();
  for (const [name, viewport] of [['desktop', {width: 1400, height: 900}], ['phone', {width: 390, height: 780}]]) {
    const ctx = await b.newContext({ extraHTTPHeaders: {"Tailscale-User-Login": process.env.ML_LOGIN || "me@x"}, viewport, hasTouch: name === 'phone' });
    const p = await ctx.newPage(); const errs = []; p.on('pageerror', e => errs.push(e.message));
    await p.goto(BASE); await p.waitForFunction(() => LIB.shelves.length && document.querySelector('#kinds [aria-current=true]'));
    const barTop = () => p.evaluate(() => { const r = document.querySelector('header.top').getBoundingClientRect(); return {top: Math.round(r.top), bottom: Math.round(r.bottom), y: Math.round(scrollY)}; });
    for (const kind of ['book', 'movie', 'series']) {
      await p.click('#kind-' + kind); await p.evaluate(() => scrollTo(0, 0));
      await p.mouse.wheel(0, 2500); await p.waitForTimeout(250);
      const r = await barTop();
      ok(r.y > 0 ? r.top >= 0 && r.top <= 2 && r.bottom > 20 : true, `${name} ${kind}: the top bar is on screen after scrolling ${JSON.stringify(r)}`);
      ok(await p.evaluate(() => { const r = document.querySelector('header.top').getBoundingClientRect();
        const el = document.elementFromPoint(Math.min(innerWidth - 2, r.left + r.width / 2), r.top + Math.min(10, r.height / 2));
        return !!el && !!el.closest('header.top'); }), `${name} ${kind}: nothing covers the top bar`);
      const t = await p.evaluate(() => { const h = document.querySelector('header.top').getBoundingClientRect(), b = document.getElementById('dbhead').getBoundingClientRect();
        return {gap: Math.round(b.top - h.bottom), h: Math.round(b.height), vh: innerHeight}; });
      ok(Math.abs(t.gap) <= 1, `${name} ${kind}: the page's toolbar sits right under the top bar after scrolling ${JSON.stringify(t)}`);
      if (process.env.SHOTS) await p.screenshot({path: `${process.env.SHOTS}/${name}-${kind}.png`});
      ok(t.h < t.vh * 0.3, `${name} ${kind}: the toolbar takes under 30% of the screen ${JSON.stringify(t)}`);
    }
    // Visuals: its toolbar (tabs, Page filter) stays too
    await p.click('[data-sec="visuals"]'); await p.waitForSelector('#v-visuals .vhead');
    await p.evaluate(() => scrollTo(0, 0)); await p.mouse.wheel(0, 1500); await p.waitForTimeout(250);
    const vt = await p.evaluate(() => { const h = document.querySelector('header.top').getBoundingClientRect(), b = document.querySelector('#v-visuals .vhead').getBoundingClientRect(); return {gap: Math.round(b.top - h.bottom), y: Math.round(scrollY)}; });
    ok(vt.y === 0 || Math.abs(vt.gap) <= 1, `${name} visuals: the Page filter bar sits under the top bar ${JSON.stringify(vt)}`);
    await p.click('[data-sec="database"]');
    // collection strip (a database without a two-film collection gets one in the page, for this check only)
    await p.click('#kind-movie');
    let film = await p.evaluate(() => { const ids = {}; for (const i of LIB.items) if (i.kind === 'movie' && i.coll) (ids[i.coll.id] ||= []).push(i.id);
      const c = Object.values(ids).find(v => v.length >= 2); return c ? c[0] : null; });
    if (!film) {   // no two-film collection here: three films get one in the page and in their detail answers, for this check only
      const ids = await p.evaluate(() => { const m = LIB.items.filter(i => i.kind === 'movie').slice(0, 3); if (m.length < 2) return [];
        for (const i of m) i.coll = {id: 'uicheck', name: 'Check collection'}; return m.map(i => i.id); });
      if (ids.length) {
        await p.route(/\/api\/items\/[^/?]+$/, async route => {
          const res = await route.fetch(); const j = await res.json();
          if (ids.includes(decodeURIComponent(route.request().url().split('/').pop()))) j.coll = {id: 'uicheck', name: 'Check collection'};
          await route.fulfill({response: res, json: j});
        });
        film = ids[0];
      }
    }
    if (!film) { console.log('SKIP collection strip [fewer than two films in this database]'); }
    else {
      await p.evaluate(id => openDetail(id), film);
      await p.waitForSelector('.collstrip .cs-film[aria-current="true"]');
      const n = await p.locator('.collstrip .cs-film').count();
      ok(n >= 2, `${name}: the film's detail lists the collection's ${n} films`);
      const other = await p.locator('.collstrip .cs-film:not([aria-current])').first().getAttribute('data-open');
      await p.locator('.collstrip .cs-film:not([aria-current])').first().click();
      await p.waitForFunction(id => st.selected === id && document.querySelector('.collstrip .cs-film[aria-current="true"]')?.dataset.open === id, other);
      ok(true, `${name}: choosing another film of the collection opens it, its strip marks it`);
      const hasNav = await p.locator('#cs-prev, #cs-next').count();
      ok(hasNav >= 1, `${name}: previous or next film button is there`);
      if (name === 'phone') {
        await p.mouse.wheel(0, 800); await p.waitForTimeout(200);
        const r = await barTop(); ok(r.top >= 0 && r.top <= 2, `phone: the top bar stays with the detail open ${JSON.stringify(r)}`);
      }
    }
    ok(errs.length === 0, `${name}: no page errors` + (errs.length ? ': ' + errs.join(' | ') : ''));
    await ctx.close();
  }
  console.log(fails ? fails + ' FAILED' : 'ALL PASS');
  await b.close();
})();
