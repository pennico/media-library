"""Copy the ebook files imported from CWA into the library's own store.

    python -m medialib adopt-files SNAPSHOT_DIR [--apply]

The importer records CWA's files where they are (``storage = 'cwa'``, a path
inside the Calibre library). To serve them over OPDS without reaching into
CWA, each one is copied from the snapshot into ``media/files/<sha[:2]>/<sha>.<ext>``
(the same place as uploads), checked against the sha256 recorded at import,
and switched to ``storage = 'store'``. Idempotent; locked books are left as
they are (only the owner changes those: upload the file in the console).
"""
import os
import shutil

import psycopg

from . import db, files


def plan(conn, library, media_dir):
    out = []
    root = os.path.realpath(library)
    for r in conn.execute("""SELECT f.id, f.rel_path, f.sha256, f.format, i.title, i.locked
                               FROM catalog.edition_file f JOIN catalog.edition e ON e.id = f.edition_id
                               JOIN catalog.media_item i ON i.id = e.work_id
                              WHERE f.storage = 'cwa' AND f.deleted_at IS NULL ORDER BY i.title""").fetchall():
        src = os.path.realpath(os.path.join(root, r["rel_path"]))
        rel = "files/{}/{}.{}".format(r["sha256"][:2], r["sha256"], r["format"])
        step = dict(r, src=src, rel=rel, dest=os.path.join(media_dir, rel), problem=None)
        if r["locked"]:
            step["problem"] = "locked: upload the file in the console instead"
        elif not src.startswith(root + os.sep) or not os.path.isfile(src):
            step["problem"] = "not in the snapshot"
        elif files.sha256_file(src) != r["sha256"]:
            step["problem"] = "the snapshot's file differs from the one imported (sha256)"
        out.append(step)
    return out


def copy(step):
    dest = step["dest"]
    if os.path.exists(dest) and files.sha256_file(dest) == step["sha256"]:
        return
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"
    shutil.copyfile(step["src"], tmp)
    os.chmod(tmp, 0o640)
    if files.sha256_file(tmp) != step["sha256"]:
        os.remove(tmp)
        raise OSError("copy of {} does not match its sha256".format(step["rel_path"]))
    os.replace(tmp, dest)


def main(cfg, snapshot, apply=False, log=print):
    library = os.path.join(snapshot, "calibre-library")
    with db.connect(cfg, db.ROLE_APP) as conn:
        steps = plan(conn, library, cfg.media_dir)
        moved, problems = 0, 0
        for s in steps:
            if s["problem"]:
                problems += 1
                log("skip  {}: {}".format(s["title"], s["problem"]))
                continue
            if not apply:
                log("copy  {} -> {}".format(s["title"], s["rel"]))
                continue
            try:
                copy(s)
                with db.write_tx(conn, "system", "adopt-files", reason="CWA file copied into the store"):
                    conn.execute("UPDATE catalog.edition_file SET storage = 'store', rel_path = %s"
                                 " WHERE id = %s AND storage = 'cwa'", (s["rel"], s["id"]))
                moved += 1
            except (OSError, psycopg.Error) as e:
                problems += 1
                log("FAIL  {}: {}".format(s["title"], getattr(getattr(e, "diag", None), "message_primary", None) or e))
        left = sum(1 for s in steps if not s["problem"]) - moved
        log("{} copied into the store, {} skipped{}".format(
            moved, problems, "" if apply else "; dry run: {} would be copied".format(left)))
    return 0
