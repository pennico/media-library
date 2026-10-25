# foliate-js (vendored)

EPUB rendering for the in-browser reader (`web/reader.html` + `web/reader.js`, served at `/read?file=<uuid>`).
Safari cannot open EPUB files, so the console renders them itself. No CDN at runtime: these files are served
by medialib from `/vendor/foliate-js/` as `text/javascript`, under the reader page's `script-src 'self'` CSP.

- Upstream: https://github.com/johnfactotum/foliate-js
- Pinned commit: `78914aef4466eb960965702401634c2cb348e9b1` (2026-05-01, "Use original hrefs for external
  links and add isExternal in fb2.js (#129)"), the head of `main` when vendored on 2026-10-25.
- License: MIT, Copyright (c) 2022 John Factotum. See `LICENSE` (upstream file, unchanged).
- `vendor/zip.js` is upstream's minified bundle of zip.js 2.8.22 (`@zip.js/zip.js`, the `zip-core` entry),
  BSD-3-Clause, Copyright (c) 2023 Gildas Lormeau. Its license text is in `vendor/zip.js.LICENSE`, taken from
  the npm tarball whose sha512 matches upstream's `package-lock.json`.

All files are byte-identical to the pinned commit; nothing is patched.

## Files copied, and why

| File | Bytes | sha256 (first 16) | Why |
|---|---:|---|---|
| `view.js` | 22850 | e0093af921feb03b | `<foliate-view>` element, the reader's entry point |
| `epubcfi.js` | 13233 | e1a688ad5c84bf31 | static import of `view.js` and `epub.js`; CFI positions (saved reading position) |
| `progress.js` | 4255 | 544f5e90babc0cb3 | static import of `view.js`; percentage, location, current TOC item |
| `overlayer.js` | 7051 | d3d3396e3083277c | static import of `view.js` (annotation overlays) |
| `text-walker.js` | 1531 | 4ad740ab6142b5be | static import of `view.js` (search/TTS text walking) |
| `epub.js` | 43685 | dfe347495039e9b5 | dynamic import of `view.js` for EPUB files |
| `paginator.js` | 44222 | 51cec81572d5d182 | dynamic import of `view.js` for reflowable books (the paginated renderer) |
| `fixed-layout.js` | 12008 | 7aee1d15d8e0479b | dynamic import of `view.js` for fixed-layout (`pre-paginated`) EPUBs |
| `vendor/zip.js` | 36512 | 2a0d43157e4a447c | dynamic import of `view.js`; reads the EPUB zip (native `DecompressionStream`, no workers, no wasm) |
| `LICENSE` | 1070 | b621ffc351fe263b | foliate-js MIT license |
| `vendor/zip.js.LICENSE` | 1501 | 1b7ebc8d7889ed25 | zip.js BSD-3-Clause license |

Total 187,918 bytes (about 184 KiB, unminified apart from zip.js).

## Not copied

`view.js` also has dynamic imports for formats and features the reader does not use: `comic-book.js`,
`fb2.js`, `mobi.js`, `pdf.js` (+ `vendor/pdfjs/`), `vendor/fflate.js`, `search.js`, `tts.js`. They are
only fetched when a non-EPUB file or search/TTS is opened. `reader.js` checks the zip signature before
calling foliate and names the file `book.epub`, so those paths are never taken; if one ever were, the import
fails with a 404 and the reader shows its error card. Also not copied: upstream's demo (`reader.html`,
`reader.js`, `ui/`), `footnotes.js`, `dict.js`, `opds.js`, `quote-image.js`, `uri-template.js`, tests,
build files.

## Security note

foliate renders each book section in a same-origin `blob:` iframe with `sandbox="allow-same-origin
allow-scripts"` (needed for WebKit bug 218086), so the sandbox does not stop scripts inside an EPUB. What
stops them is the reader page's CSP, which those iframes inherit: it must keep `script-src 'self'` with no
`'unsafe-inline'`, `blob:` or hashes. See the CSP comment in `web/reader.js`'s header and the server route.

## Updating

Pick a newer upstream commit at least two weeks old, copy the same files, re-check the import graph
(`grep -n "import" view.js epub.js paginator.js fixed-layout.js`), update the table above, and re-test
the reader in Safari.
