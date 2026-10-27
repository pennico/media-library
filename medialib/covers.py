"""Content-addressed cover store (DESIGN.md §4.2, D18).

Files live at <media_dir>/covers/<sha[:2]>/<sha>.<ext>; the database stores
the relative path. Every image is checked with Pillow (a real JPEG/PNG/WebP,
bounded size), so placeholders (SVG) and error pages never become covers.
Downloads are remembered in <media_dir>/cache/fetch-index.json, so a URL is
fetched once.
"""
import hashlib
import io
import json
import os
import tempfile
import threading
import time
import urllib.error
import urllib.request

from PIL import Image

MAX_BYTES = 15 * 1024 * 1024
MIME = {"JPEG": ("image/jpeg", "jpg"), "PNG": ("image/png", "png"), "WEBP": ("image/webp", "webp")}
USER_AGENT = "media-library/0.1 (personal library; covers fetched once)"


class CoverError(Exception):
    pass


def inspect(data):
    """Validate image bytes -> {mime, ext, width, height}."""
    if not data or len(data) > MAX_BYTES:
        raise CoverError("empty or larger than {} bytes".format(MAX_BYTES))
    try:
        with Image.open(io.BytesIO(data)) as im:
            fmt = im.format
            im.verify()
        with Image.open(io.BytesIO(data)) as im:
            width, height = im.size
    except Exception as e:  # noqa: BLE001 - Pillow raises many kinds
        raise CoverError("not a readable image: {}".format(e)) from e
    if fmt not in MIME:
        raise CoverError("unsupported image format {}".format(fmt))
    if width < 16 or height < 16:
        raise CoverError("image too small ({}x{}): a placeholder".format(width, height))
    mime, ext = MIME[fmt]
    return {"mime": mime, "ext": ext, "width": width, "height": height}


class CoverStore:
    def __init__(self, media_dir, offline=False, pause=0.5, log=print):
        self.root = os.path.abspath(media_dir)
        self.offline = offline
        self.pause = pause
        self.log = log
        self._index_path = os.path.join(self.root, "cache", "fetch-index.json")
        try:
            with open(self._index_path, encoding="utf-8") as fh:
                self._index = json.load(fh)
        except (OSError, ValueError):
            self._index = {}
        self.fetched = 0

    def path(self, rel):
        return os.path.join(self.root, rel)

    def put(self, data):
        meta = inspect(data)
        sha = hashlib.sha256(data).hexdigest()
        rel = "covers/{}/{}.{}".format(sha[:2], sha, meta["ext"])
        dest = self.path(rel)
        if not os.path.exists(dest):
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dest), prefix=".tmp-")
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o644)
            os.replace(tmp, dest)
        return {"sha256": sha, "local_path": rel, "mime": meta["mime"], "width": meta["width"],
                "height": meta["height"], "bytes": len(data)}

    def put_file(self, path):
        with open(path, "rb") as fh:
            return self.put(fh.read())

    def fetch(self, url):
        """Download once; returns the stored image info (+ fetched_at)."""
        hit = self._index.get(url)
        if hit and os.path.exists(self.path(hit["local_path"])):
            return hit
        if self.offline:
            raise CoverError("offline and not cached")
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read(MAX_BYTES + 1)
        except (urllib.error.URLError, OSError) as e:
            raise CoverError("download failed: {}".format(e)) from e
        finally:
            time.sleep(self.pause)
        info = dict(self.put(data), fetched_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        self._index[url] = info
        self.fetched += 1
        self._save_index()
        return info

    def _save_index(self):
        os.makedirs(os.path.dirname(self._index_path), exist_ok=True)
        tmp = self._index_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._index, fh, indent=0, sort_keys=True)
        os.replace(tmp, self._index_path)


def make_thumb(src, dest, width):
    """A JPEG of the cover at ``src``, ``width`` px wide (or the original's width, if narrower: never enlarged),
    written atomically to ``dest``. Safe to run twice at once for the same file: each writer has its own temporary
    name, and the last rename wins with the same picture."""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with Image.open(src) as im:
        width = min(width, im.width)
        h = max(1, round(im.height * width / im.width))
        im.draft("RGB", (width, h))          # a JPEG is decoded at 1/2..1/8 scale, never below the size wanted
        im = im.convert("RGB").resize((width, h), Image.LANCZOS)
        tmp = "{}.{}.tmp".format(dest, threading.get_ident())
        try:
            im.save(tmp, "JPEG", quality=78, optimize=True)     # baseline: decodes faster than progressive
            os.replace(tmp, dest)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
