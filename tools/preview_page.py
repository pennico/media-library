#!/usr/bin/env python3
"""Build the claude.ai preview of the console: the real web/index.html with
the library, every book's detail, the statistics and small cover
thumbnails embedded, answered by the page's in-browser store (D29).

    python3 tools/preview_page.py [--db medialib_dev] [--out out/review/index.html]

The output holds the owner's library data: it goes to out/ (gitignored).
"""
import argparse
import base64
import hashlib
import io
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from PIL import Image  # noqa: E402

import devdb  # noqa: E402
from medialib import config, db  # noqa: E402
from medialib.web import queries  # noqa: E402


def thumb(path, width=240):
    with Image.open(path) as im:
        im = im.convert("RGB")
        im = im.resize((width, max(1, round(im.height * width / im.width))), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=72, optimize=True, progressive=True)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def page_parts(html):
    """Split web/index.html into what an artifact page may contain (no
    doctype/html/head/body: the publisher adds its own skeleton)."""
    title = re.search(r"<title>.*?</title>", html, re.S).group(0)
    meta = re.search(r'<meta name="claude:viewport-fit-sides"[^>]*>', html).group(0)
    style = re.search(r"<style>.*?</style>", html, re.S).group(0)
    body = re.search(r"<body>(.*)</body>", html, re.S).group(1)
    return title, meta, style, body


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="medialib_dev")
    ap.add_argument("--out", default=os.path.join(ROOT, "out", "review", "index.html"))
    a = ap.parse_args(argv)
    for k, v in (line.replace("export ", "", 1).split("=", 1) for line in devdb.env_lines().splitlines()):
        os.environ.setdefault(k, v)
    cfg = config.from_env(db_name=a.db, media_dir=os.path.join(ROOT, "media"))
    with db.connect(cfg) as conn, db.connect(cfg, db.ROLE_READONLY) as ro:
        library = queries.library(conn)
        details = {i["id"]: queries.detail(conn, i["id"]) for i in library["items"]}
        stats = queries.stats(ro, "book")
        paths = {r["sha256"]: r["local_path"] for r in conn.execute(
            "SELECT DISTINCT sha256, local_path FROM catalog.cover WHERE local_path IS NOT NULL AND deleted_at IS NULL")}
    wanted = set()
    for i in library["items"]:
        if i["cover"]:
            wanted.add(re.search(r"[0-9a-f]{64}", i["cover"]).group(0))
    for d in details.values():
        for c in d["covers"]:
            if c["thumb"]:
                wanted.add(re.search(r"[0-9a-f]{64}", c["thumb"]).group(0))
    covers = {sha: thumb(os.path.join(cfg.media_dir, paths[sha])) for sha in sorted(wanted) if sha in paths}
    data = {"library": library, "details": details, "stats": stats, "covers": covers}
    blob = json.dumps(data, ensure_ascii=False, default=str, sort_keys=True)
    data["version"] = hashlib.sha256(blob.encode()).hexdigest()[:12]
    with open(os.path.join(ROOT, "web", "index.html"), encoding="utf-8") as fh:
        title, meta, style, body = page_parts(fh.read())
    inject = "<script>window.ML_PREVIEW = {};</script>\n".format(
        json.dumps(data, ensure_ascii=False, default=str).replace("</", "<\\/"))
    body = body.replace("<script>\n\"use strict\";", inject + "<script>\n\"use strict\";", 1)
    assert "window.ML_PREVIEW" in body
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as fh:
        fh.write(title + "\n" + meta + "\n" + style + "\n" + body)
    print(a.out, "{:.1f} MB".format(os.path.getsize(a.out) / 1e6), len(library["items"]), "items,", len(covers), "covers")


if __name__ == "__main__":
    sys.exit(main())
