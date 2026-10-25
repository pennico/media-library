"""Read Joplin sync items (unencrypted) from a sync target folder.

Joplin stores every note and notebook as one ``<id>.md`` file:

    <title>
    <blank>
    <body ...>            (notes only)
    <blank>
    id: 0123...
    parent_id: ...
    type_: 1              (1 = note, 2 = notebook)
    updated_time: 2026-10-18T17:00:00.000Z
    ...

The trailing ``key: value`` block is the metadata. This module only reads;
nothing is ever written to the sync folder (no sync conflicts). The folder
comes as a directory or as a tar stream (e.g. ``tar -cf - .`` of the folder
piped in), so the reader needs no access to wherever Joplin syncs to.
"""
import os
import re
import tarfile

TYPE_NOTE = "1"
TYPE_FOLDER = "2"
MAX_ITEM_BYTES = 2 * 1024 * 1024        # a note bigger than this is not a review

_META_LINE = re.compile(r"^([a-z_]+): ?(.*)$")


def parse_item(text):
    """One sync item -> dict with title, body and the metadata keys, or None
    when the text has no metadata block (not a Joplin item)."""
    lines = text.replace("\r\n", "\n").split("\n")
    while lines and lines[-1] == "":
        lines.pop()
    meta = {}
    i = len(lines) - 1
    while i >= 0 and _META_LINE.match(lines[i]):
        key, value = _META_LINE.match(lines[i]).groups()
        meta[key] = value
        i -= 1
    if "id" not in meta or "type_" not in meta:
        return None
    head = lines[:i + 1]
    while head and head[-1] == "":
        head.pop()
    item = dict(meta)
    item["title"] = head[0] if head else ""
    body = head[1:]
    if body and body[0] == "":
        body = body[1:]
    item["body"] = "\n".join(body)
    return item


def is_live(item):
    """Not in the trash, not a conflict copy, not encrypted."""
    return (item.get("deleted_time", "0") in ("", "0")
            and item.get("is_conflict", "0") in ("", "0")
            and item.get("encryption_applied", "0") in ("", "0"))


def _wanted(name):
    base = os.path.basename(name)
    return base.endswith(".md") and re.fullmatch(r"[0-9a-f]{32}\.md", base) is not None


def load_dir(path):
    """Items from the sync folder (or a copy of it)."""
    items = []
    for name in sorted(os.listdir(path)):
        full = os.path.join(path, name)
        if not _wanted(name) or not os.path.isfile(full) or os.path.getsize(full) > MAX_ITEM_BYTES:
            continue
        with open(full, encoding="utf-8", errors="replace") as fh:
            item = parse_item(fh.read())
        if item:
            items.append(item)
    return items


def load_tar(fileobj):
    """Items from a tar stream of the sync folder (read as a stream: stdin works)."""
    items = []
    with tarfile.open(fileobj=fileobj, mode="r|*") as tar:
        for member in tar:
            if not (member.isfile() and _wanted(member.name)) or member.size > MAX_ITEM_BYTES:
                continue
            fh = tar.extractfile(member)
            item = parse_item(fh.read().decode("utf-8", "replace")) if fh else None
            if item:
                items.append(item)
    return items


def notes_in_notebook(items, notebook_title):
    """Live notes in the named notebook and all its sub-notebooks.

    Returns [(note, path)] where path is the list of notebook titles from
    the named notebook down to the note's own notebook.
    """
    folders = {i["id"]: i for i in items
               if i.get("type_") == TYPE_FOLDER and is_live(i)}
    roots = [f for f in folders.values()
             if f["title"].strip().lower() == notebook_title.strip().lower()]
    if not roots:
        return []
    root_ids = {r["id"] for r in roots}

    def path_to_root(folder_id):
        path = []
        seen = set()
        while folder_id in folders and folder_id not in seen:
            seen.add(folder_id)
            path.append(folders[folder_id]["title"])
            if folder_id in root_ids:
                return list(reversed(path))
            folder_id = folders[folder_id].get("parent_id", "")
        return None

    out = []
    for item in items:
        if item.get("type_") != TYPE_NOTE or not is_live(item):
            continue
        path = path_to_root(item.get("parent_id", ""))
        if path is not None:
            out.append((item, path))
    return out
