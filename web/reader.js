// In-browser EPUB reader for the media-library console.
// Called as /read?file=<edition_file public_id>; fetches api/files/<id> (same origin) and renders it with
// foliate-js (vendor/foliate-js, pinned, see VENDORED.md). This file is loaded as an external module so the
// page's CSP can keep `script-src 'self'` without 'unsafe-inline': foliate renders book sections in
// same-origin blob: iframes that inherit this page's CSP, and that CSP is what stops EPUB scripts running.
//
// CSP the server must send for /read (tested in Chromium and WebKit, with a scripted test EPUB):
//   default-src 'none'; script-src 'self'; style-src 'unsafe-inline' blob:; img-src blob: data:;
//   font-src blob: data:; media-src blob:; connect-src 'self'; frame-src blob:; base-uri 'none';
//   form-action 'none'; frame-ancestors 'self'
// Never add 'unsafe-inline' or blob: to script-src. frame-ancestors must be 'self', not 'none': WebKit
// (Safari) applies the inherited policy to the blob: section iframes and refuses to load them.

// epub.js uses Object.groupBy / Map.groupBy (Safari 17.4+); tiny fallbacks so Safari 16.4-17.3 still works.
const define = (obj, name, fn) => { if (typeof obj[name] !== 'function') Object.defineProperty(obj, name, { value: fn, writable: true, configurable: true }) }
define(Object, 'groupBy', (items, key) => {
    const out = Object.create(null); let i = 0
    for (const x of items) { const k = key(x, i++); (out[k] ??= []).push(x) }
    return out
})
define(Map, 'groupBy', (items, key) => {
    const out = new Map(); let i = 0
    for (const x of items) { const k = key(x, i++); const list = out.get(k); if (list) list.push(x); else out.set(k, [x]) }
    return out
})

const $ = id => document.getElementById(id)
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
const SIZES = [14, 15, 16, 17, 18, 19, 20, 22, 24, 26, 28, 32]
const SERIF = '"Iowan Old Style", "Palatino Linotype", Palatino, "Book Antiqua", Georgia, "Noto Serif", "DejaVu Serif", serif'

// Same convention as index.html: "ml-" prefix, JSON values, never throws (private mode, blocked storage).
const store = {
    get(key, dflt) { try { const v = localStorage.getItem('ml-' + key); return v == null ? dflt : JSON.parse(v) } catch { return dflt } },
    set(key, v) { try { localStorage.setItem('ml-' + key, JSON.stringify(v)) } catch { /* storage unavailable */ } },
    remove(key) { try { localStorage.removeItem('ml-' + key) } catch { /* storage unavailable */ } },
}

// foliate reports some failures only as uncaught errors (e.g. a section iframe the browser refuses to load,
// which is what a CSP with frame-ancestors 'none' does in Safari) and its promise then never settles.
// Race the first render against those errors and a timeout so the page shows an error, not an endless spinner.
function watchdog(ms) {
    const dog = { fired: false, cleanup: () => {} }
    dog.promise = new Promise((_, reject) => {
        const fire = err => { dog.fired = true; reject(err) }
        const onError = e => fire(e.error ?? new Error(e.message || 'Script error'))
        const onRejection = e => fire(e.reason ?? new Error('Unhandled rejection'))
        const timer = setTimeout(() => fire(new Error(`No page was shown after ${ms / 1000} s`)), ms)
        addEventListener('error', onError)
        addEventListener('unhandledrejection', onRejection)
        dog.cleanup = () => { clearTimeout(timer); removeEventListener('error', onError); removeEventListener('unhandledrejection', onRejection) }
    })
    dog.promise.catch(() => {})
    return dog
}

const narrow = matchMedia('(max-width: 560px)')
const darkMQ = matchMedia('(prefers-color-scheme: dark)')
const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)')
const isDark = () => {
    const t = document.documentElement.dataset.theme
    return t ? t === 'dark' : darkMQ.matches
}

const ui = {
    title: $('title'), stage: $('stage'), status: $('status'), statusText: $('status-text'),
    tocBtn: $('toc-btn'), toc: $('toc'), tocList: $('toc-list'), tocClose: $('toc-close'), scrim: $('scrim'),
    dec: $('font-dec'), inc: $('font-inc'), prev: $('prev'), next: $('next'),
    fill: $('fill'), pct: $('pct'), chap: $('chap'), loc: $('loc'),
}

let view = null
let fileId = ''
let sizeIndex = nearestSize(store.get('reader-font', null) ?? (narrow.matches ? 17 : 18))
const tocButtons = new Map()
let currentTocHref = null

function nearestSize(px) {
    let best = 0
    SIZES.forEach((s, i) => { if (Math.abs(s - px) < Math.abs(SIZES[best] - px)) best = i })
    return best
}

// ------------------------------------------------------------------ status and errors

function setStatus(text) {
    ui.statusText.textContent = text
}

function fail(heading, message, detail) {
    const card = document.createElement('div')
    card.className = 'card err'
    const h = document.createElement('h2'); h.textContent = heading
    const p = document.createElement('p'); p.textContent = message
    card.append(h, p)
    if (detail) { const d = document.createElement('p'); d.className = 'detail'; d.textContent = detail; card.append(d) }
    const a = document.createElement('a'); a.href = './'; a.textContent = '‹ Back to the library'
    card.append(a)
    ui.status.replaceChildren(card)
    ui.status.setAttribute('role', 'alert')
    ui.status.hidden = false
    ui.title.textContent = 'Reader'
    for (const b of [ui.tocBtn, ui.dec, ui.inc, ui.prev, ui.next]) b.disabled = true
    view?.remove()
    view = null
}

// ------------------------------------------------------------------ book styles

function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim()
}

// Injected after the book's own CSS. Same-specificity rules win over the book's (`body` font, paragraph
// spacing); class-level publisher choices (.sans, embedded fonts, centred headings) still apply.
// Dark mode forces the palette, including -webkit-text-fill-color, which some publishers set on links.
function bookStyles() {
    const dark = isDark()
    const bg = cssVar('--bg'), fg = cssVar('--fg'), accent = cssVar('--accent'), soft = cssVar('--accent-soft')
    return `
        @namespace epub "http://www.idpf.org/2007/ops";
        html { color-scheme: ${dark ? 'dark' : 'light'}; background: ${bg} !important; color: ${fg} !important;
            font-size: ${SIZES[sizeIndex]}px !important; -webkit-text-size-adjust: none; text-size-adjust: none; }
        body { background: transparent !important; font-family: ${SERIF}; }
        p, li, blockquote, dd {
            line-height: 1.5 !important;
            text-align: justify;
            -webkit-hyphens: auto; hyphens: auto;
            -webkit-hyphenate-limit-before: 3; -webkit-hyphenate-limit-after: 2; -webkit-hyphenate-limit-lines: 2;
            hanging-punctuation: allow-end last;
            widows: 2; orphans: 2;
        }
        [align="left"] { text-align: left; } [align="right"] { text-align: right; }
        [align="center"] { text-align: center; } [align="justify"] { text-align: justify; }
        pre { white-space: pre-wrap !important; }
        a:link, a:visited { color: ${accent}${dark ? ' !important' : ''}; }
        ::selection { background: ${soft}; }
        aside[epub|type~="endnote"], aside[epub|type~="footnote"], aside[epub|type~="note"], aside[epub|type~="rearnote"] { display: none; }
        ${dark ? `
        body *:not(a) { color: inherit !important; }
        body, body * { background-color: transparent !important; -webkit-text-fill-color: currentColor !important; }
        ` : ''}
    `
}

function applyStyles() {
    view?.renderer?.setStyles?.(bookStyles())
}

function applyLayout() {
    const r = view?.renderer
    if (!r || view.isFixedLayout) return
    const phone = narrow.matches
    r.setAttribute('flow', 'paginated')
    r.setAttribute('gap', '6%')
    r.setAttribute('margin', phone ? '22px' : '36px')
    r.setAttribute('max-inline-size', '680px')
    r.setAttribute('max-column-count', '2')
    if (reducedMotion.matches) r.removeAttribute('animated')
    else r.setAttribute('animated', '')
}

function setSize(delta) {
    const i = Math.min(SIZES.length - 1, Math.max(0, sizeIndex + delta))
    if (i === sizeIndex) return
    sizeIndex = i
    store.set('reader-font', SIZES[i])
    updateSizeButtons()
    applyStyles()
}

function updateSizeButtons() {
    const fixed = !view || view.isFixedLayout || !view.renderer?.setStyles
    ui.dec.disabled = fixed || sizeIndex === 0
    ui.inc.disabled = fixed || sizeIndex === SIZES.length - 1
    ui.dec.title = `Smaller text (${SIZES[sizeIndex]} px)`
    ui.inc.title = `Larger text (${SIZES[sizeIndex]} px)`
}

// ------------------------------------------------------------------ table of contents

function buildToc(toc) {
    tocButtons.clear()
    const make = items => {
        const ol = document.createElement('ol')
        for (const item of items) {
            const li = document.createElement('li')
            const b = document.createElement('button')
            b.type = 'button'
            b.className = 'it'
            b.textContent = (item.label ?? '').trim() || 'Untitled'
            if (item.href) {
                tocButtons.set(item.href, b)
                b.addEventListener('click', () => { closeToc(false); view.goTo(item.href) })
            } else b.disabled = true
            li.append(b)
            if (item.subitems?.length) li.append(make(item.subitems))
            ol.append(li)
        }
        return ol
    }
    if (toc?.length) ui.tocList.replaceChildren(make(toc))
    else {
        const p = document.createElement('p'); p.className = 'empty'; p.textContent = 'This book has no table of contents.'
        ui.tocList.replaceChildren(p)
    }
}

function markToc(href) {
    if (href === currentTocHref) return
    tocButtons.get(currentTocHref)?.removeAttribute('aria-current')
    currentTocHref = href
    tocButtons.get(href)?.setAttribute('aria-current', 'true')
}

function openToc() {
    ui.scrim.hidden = false
    ui.toc.hidden = false
    ui.tocBtn.setAttribute('aria-expanded', 'true')
    const cur = tocButtons.get(currentTocHref)
    if (cur) { cur.scrollIntoView({ block: 'center' }); cur.focus({ preventScroll: true }) }
    else ui.tocClose.focus()
}

function closeToc(restoreFocus = true) {
    if (ui.toc.hidden) return
    ui.scrim.hidden = true
    ui.toc.hidden = true
    ui.tocBtn.setAttribute('aria-expanded', 'false')
    if (restoreFocus) ui.tocBtn.focus()
}

// ------------------------------------------------------------------ navigation

function onKey(e) {
    if (!view || e.defaultPrevented || e.altKey || e.ctrlKey || e.metaKey) return
    if (!ui.toc.hidden) { if (e.key === 'Escape') { e.preventDefault(); closeToc() } return }
    const onControl = e.target?.closest?.('button, a, input, select, textarea')
    switch (e.key) {
        case 'ArrowLeft': view.goLeft(); break
        case 'ArrowRight': view.goRight(); break
        case 'PageUp': view.prev(); break
        case 'PageDown': view.next(); break
        case ' ': if (onControl) return; if (e.shiftKey) view.prev(); else view.next(); break
        default: return
    }
    e.preventDefault()
}

// Tap or click on the outer 30 % of the reading area turns the page; the middle is left for selecting text.
function edgeTurn(clientX) {
    const r = ui.stage.getBoundingClientRect()
    const f = (clientX - r.left) / r.width
    if (f < 0.3) view.goLeft()
    else if (f > 0.7) view.goRight()
}

function onSectionLoad({ detail: { doc } }) {
    doc.addEventListener('keydown', onKey)
    doc.addEventListener('click', e => {
        if (e.defaultPrevented || e.button !== 0) return
        if (e.target?.closest?.('a[href], button, input, select, textarea, label, summary, audio, video')) return
        const sel = doc.getSelection?.()
        if (sel && !sel.isCollapsed) return
        const frame = doc.defaultView?.frameElement
        if (frame) edgeTurn(frame.getBoundingClientRect().left + e.clientX)
    })
}

function onRelocate({ detail }) {
    const { fraction, location, tocItem, pageItem, cfi } = detail
    const f = Math.min(1, Math.max(0, fraction ?? 0))
    const pct = Math.round(f * 100)
    ui.fill.style.width = (f * 100).toFixed(2) + '%'
    ui.pct.textContent = pct + ' %'
    ui.pct.setAttribute('aria-valuenow', String(pct))
    ui.chap.textContent = (tocItem?.label ?? '').trim()
    ui.loc.textContent = pageItem?.label ? `Page ${pageItem.label}`
        : location?.total ? `Loc ${Math.min(location.current + 1, location.total)} of ${location.total}` : ''
    markToc(tocItem?.href ?? null)
    if (cfi) store.set('reader-pos-' + fileId, { cfi, fraction: f, at: Date.now() })
}

// ------------------------------------------------------------------ metadata

const langText = x => !x ? '' : typeof x === 'string' ? x : (x[Object.keys(x)[0]] ?? '')
const people = x => (Array.isArray(x) ? x : x ? [x] : [])
    .map(p => typeof p === 'string' ? p : langText(p?.name)).filter(Boolean).join(', ')

// ------------------------------------------------------------------ open

async function main() {
    fileId = (new URLSearchParams(location.search).get('file') ?? '').toLowerCase()
    if (!UUID_RE.test(fileId))
        return fail('No book selected', 'Open a book in the library and choose Read to start reading.')

    try {
        await import('./vendor/foliate-js/view.js')
    } catch (e) {
        return fail('The reader could not start', 'Its script files did not load. Reload the page; if it keeps happening the server is missing vendor/foliate-js.', String(e))
    }

    setStatus('Downloading the book…')
    let res
    try {
        res = await fetch('api/files/' + fileId, { credentials: 'same-origin', headers: { Accept: 'application/epub+zip' } })
    } catch (e) {
        return fail('Could not reach the library', 'Check the connection (Tailscale) and reload the page.', String(e))
    }
    if (!res.ok) {
        let msg = ''
        try { msg = (await res.json()).error ?? '' } catch { /* not JSON */ }
        const heading = res.status === 404 ? 'Book file not found' : res.status === 401 || res.status === 403 ? 'Not allowed' : 'The book could not be loaded'
        return fail(heading, msg || 'The server did not return the file.', `HTTP ${res.status} ${res.statusText}`.trim())
    }
    let blob
    try { blob = await res.blob() } catch (e) { return fail('The download was interrupted', 'Reload the page to try again.', String(e)) }
    const head = new Uint8Array(await blob.slice(0, 4).arrayBuffer())
    if (!(head[0] === 0x50 && head[1] === 0x4b && head[2] === 0x03 && head[3] === 0x04))
        return fail('This file is not an EPUB', 'The reader opens EPUB files only. Download the file from the library to read it in another app.',
            `Content-Type: ${res.headers.get('content-type') || 'unknown'}, ${blob.size} bytes`)

    setStatus('Opening the book…')
    view = document.createElement('foliate-view')
    ui.stage.prepend(view)
    try {
        // The name keeps foliate on its EPUB path (it picks CBZ/FBZ by file name).
        await view.open(new File([blob], 'book.epub', { type: 'application/epub+zip' }))
    } catch (e) {
        return fail('This EPUB could not be opened', 'The file may be damaged or not a valid EPUB.', e?.message || String(e))
    }

    const { book } = view
    // A broken resource (image, stylesheet) should not blank the whole section.
    book.transformTarget?.addEventListener('data', ({ detail }) => {
        detail.data = Promise.resolve(detail.data).catch(e => { console.warn('Failed to load ' + detail.name, e); return '' })
    })

    const title = langText(book.metadata?.title) || 'Untitled'
    const author = people(book.metadata?.author)
    ui.title.textContent = title
    if (author) {
        const by = document.createElement('span'); by.className = 'by'; by.textContent = ' · ' + author
        ui.title.append(by)
    }
    ui.title.title = author ? `${title} by ${author}` : title
    document.title = title + ' · Reader'
    if (book.dir === 'rtl') { ui.prev.setAttribute('aria-label', 'Next page'); ui.next.setAttribute('aria-label', 'Previous page') }

    buildToc(book.toc)
    applyLayout()
    applyStyles()
    view.addEventListener('load', onSectionLoad)
    view.addEventListener('relocate', onRelocate)
    view.addEventListener('click', e => { if (e.button === 0) edgeTurn(e.clientX) }) // the margins around the page
    view.addEventListener('external-link', e => {
        e.preventDefault()
        const href = e.detail?.href_ ?? ''
        if (/^(https?:|mailto:)/i.test(href)) window.open(href, '_blank', 'noopener,noreferrer')
    })

    const saved = store.get('reader-pos-' + fileId, null)
    const lastLocation = typeof saved?.cfi === 'string' ? saved.cfi : null
    const dog = watchdog(30000)
    try {
        try {
            await Promise.race([dog.promise, view.init({ lastLocation })])
        } catch (e) {
            if (!lastLocation || dog.fired) throw e
            console.warn('Could not restore the last position', e)
            await Promise.race([dog.promise, view.init({ lastLocation: null })])
        }
    } catch (e) {
        if (lastLocation) store.remove('reader-pos-' + fileId) // never let a bad saved position lock the book
        return fail('The book could not be displayed', 'The browser did not show its first page. Reload to try again.', e?.message || String(e))
    } finally {
        dog.cleanup()
    }

    ui.status.hidden = true
    ui.tocBtn.disabled = false
    ui.prev.disabled = false
    ui.next.disabled = false
    updateSizeButtons()
}

// ------------------------------------------------------------------ wiring

document.addEventListener('keydown', onKey)
ui.prev.addEventListener('click', () => view?.goLeft())
ui.next.addEventListener('click', () => view?.goRight())
ui.dec.addEventListener('click', () => setSize(-1))
ui.inc.addEventListener('click', () => setSize(1))
ui.tocBtn.addEventListener('click', () => ui.toc.hidden ? openToc() : closeToc())
ui.tocClose.addEventListener('click', () => closeToc())
ui.scrim.addEventListener('click', () => closeToc())
darkMQ.addEventListener('change', applyStyles)
narrow.addEventListener('change', applyLayout)
reducedMotion.addEventListener('change', applyLayout)

main().catch(e => {
    console.error(e)
    fail('Something went wrong', 'The reader stopped unexpectedly. Reload the page to try again.', e?.message || String(e))
})
