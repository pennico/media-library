"""Reading positions from Readest (KOReader sync protocol) and what they mean
for the owner's status.

A document id is the KOReader partial MD5 of the reader's copy. It names a
book through the stored file (``edition_file.kosync_md5``) or an alias
(CWA's older checksums, or a link the owner made). Every position is kept,
named or not, so devices sync either way. For a named book the position
also moves the reading along, as the CWA -> Yamtrack job did:

- To read (or abandoned) and the position moves by 1 % or more -> Reading
  (a new reading, started today);
- the end (99 % or more) after reading it here -> Read, finished today;
- Read, then started over (below 25 %) after it reached the end here -> a
  re-read;
- in between: the open reading's progress (and pages) follows.

These changes are the owner's own reading, so they are written as the owner
(client ``kosync:<device>``) and audited like any console edit.
"""
import datetime
import re

from psycopg.types.json import Jsonb

from .. import db
from ..web.writes import sync_status

FINISHED = 0.99
STEP = 0.01
REREAD_BELOW = 0.25


class BadDocument(ValueError):
    pass


def clean(body):
    """The PUT body -> checked values (raises BadDocument)."""
    doc = str(body.get("document") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{32}", doc):
        raise BadDocument("document must be a 32-character MD5")
    try:
        pct = float(body.get("percentage"))
    except (TypeError, ValueError):
        raise BadDocument("percentage must be a number") from None
    if not 0 <= pct <= 1.000001:
        raise BadDocument("percentage must be between 0 and 1")
    progress = str(body.get("progress") if body.get("progress") is not None else "")
    if len(progress) > 2000:
        raise BadDocument("progress is too long")
    device = " ".join(str(body.get("device") or "unknown").split())[:200] or "unknown"
    device_id = str(body.get("device_id") or "")[:200] or None
    meta = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
    meta = {k: str(meta[k])[:500] for k in ("title", "authors", "filename") if meta.get(k)}
    return doc, min(pct, 1.0), progress, device, device_id, meta


def book_for(conn, doc):
    """{edition_id, item_id, pages, title} for a document id, or None."""
    return conn.execute("""
        SELECT e.id AS edition_id, e.work_id AS item_id, e.pages, i.title
          FROM (SELECT edition_id FROM catalog.edition_file WHERE kosync_md5 = %(d)s AND deleted_at IS NULL
                UNION ALL
                SELECT edition_id FROM catalog.document_alias WHERE document = %(d)s) d
          JOIN catalog.edition e ON e.id = d.edition_id AND e.deleted_at IS NULL
          JOIN catalog.media_item i ON i.id = e.work_id AND i.deleted_at IS NULL
         LIMIT 1""", {"d": doc}).fetchone()


def get(conn, doc):
    row = conn.execute("SELECT * FROM sync.kosync_position WHERE document = %s", (doc,)).fetchone()
    if not row:
        return None
    return {"document": doc, "progress": row["progress"], "percentage": row["percentage"],
            "device": row["device"], "device_id": row["device_id"],
            "timestamp": int(row["updated_at"].timestamp())}


def put(conn, body, today=None, log=print):
    """Store a position; move the reading along. Returns (doc, timestamp)."""
    doc, pct, progress, device, device_id, meta = clean(body)
    prev = conn.execute("SELECT percentage, finished_at FROM sync.kosync_position WHERE document = %s",
                        (doc,)).fetchone()
    row = conn.execute("""INSERT INTO sync.kosync_position (document, progress, percentage, device, device_id, metadata)
                          VALUES (%s, %s, %s, %s, %s, %s)
                          ON CONFLICT (document) DO UPDATE SET progress = EXCLUDED.progress,
                              percentage = EXCLUDED.percentage, device = EXCLUDED.device,
                              device_id = EXCLUDED.device_id, updated_at = now(),
                              metadata = CASE WHEN EXCLUDED.metadata = '{}' THEN sync.kosync_position.metadata
                                              ELSE EXCLUDED.metadata END
                          RETURNING updated_at""", (doc, progress, pct, device, device_id, Jsonb(meta))).fetchone()
    book = book_for(conn, doc)
    if book:
        try:
            done = advance(conn, book, pct, prev, device, today or datetime.date.today())
            if done:
                log("kosync {}: {} ({:.0%}, {})".format(book["title"], done, pct, device))
        except Exception as e:      # noqa: BLE001 - the position is saved; the status can wait
            log("kosync {}: status not changed: {}".format(book["title"], getattr(
                getattr(e, "diag", None), "message_primary", None) or e))
    return doc, int(row["updated_at"].timestamp())


def advance(conn, book, pct, prev, device, today):
    """Readings and status for one position. Returns what changed (or '')."""
    item, edition = book["item_id"], book["edition_id"]
    prev_pct = prev["percentage"] if prev else None
    seen_end = bool(prev and prev["finished_at"])
    moved = prev_pct is None or abs(pct - prev_pct) >= STEP
    with db.write_tx(conn, "owner", "kosync:" + device[:60], reason="Readest position"):
        e = conn.execute("SELECT status FROM library.entry WHERE item_id = %s AND deleted_at IS NULL",
                         (item,)).fetchone()
        if not e:
            return ""
        status = e["status"]
        open_r = conn.execute("SELECT id, state, progress_pct, started_on FROM library.consumption WHERE item_id = %s"
                              " AND state IN ('in_progress', 'paused') AND deleted_at IS NULL", (item,)).fetchone()
        pct100 = round(pct * 100, 2)
        pages = round(pct * book["pages"]) if book["pages"] else None
        if pct >= FINISHED:
            if not seen_end:
                conn.execute("UPDATE sync.kosync_position SET finished_at = now() WHERE document IN"
                             " (SELECT kosync_md5 FROM catalog.edition_file WHERE edition_id = %s"
                             "  UNION SELECT document FROM catalog.document_alias WHERE edition_id = %s)",
                             (edition, edition))
            if open_r:
                end = max(today, open_r["started_on"] or today)
                conn.execute("UPDATE library.consumption SET state = 'completed', ended_on = %s, ended_precision = 'day',"
                             " progress_pct = 100, progress_pages = %s, progress_at = now() WHERE id = %s",
                             (end, book["pages"], open_r["id"]))
                sync_status(conn, item)
                conn.execute("UPDATE library.entry SET status = 'completed', status_changed_at = now()"
                             " WHERE item_id = %s AND status <> 'completed'", (item,))
                return "finished"
            if status in ("planned", "dropped") and prev_pct is not None and prev_pct < FINISHED:
                conn.execute("INSERT INTO library.consumption (item_id, edition_id, state, ended_on, ended_precision,"
                             " progress_pct, progress_pages, progress_at, source)"
                             " VALUES (%s, %s, 'completed', %s, 'day', 100, %s, now(), 'kosync')",
                             (item, edition, today, book["pages"]))
                conn.execute("UPDATE library.entry SET status = 'completed', status_changed_at = now() WHERE item_id = %s",
                             (item,))
                return "finished"
            return ""
        if open_r:
            changed = []
            if open_r["state"] == "paused" and moved:
                conn.execute("UPDATE library.consumption SET state = 'in_progress' WHERE id = %s", (open_r["id"],))
                changed.append("resumed")
            if open_r["progress_pct"] is None or abs(float(open_r["progress_pct"]) - pct100) >= STEP * 100:
                conn.execute("UPDATE library.consumption SET progress_pct = %s, progress_pages = %s, progress_at = now()"
                             " WHERE id = %s", (pct100, pages, open_r["id"]))
                changed.append("progress")
            if changed:
                sync_status(conn, item)
            return " + ".join(changed)
        start = (status == "planned" and pct >= STEP and moved) \
            or (status == "dropped" and prev_pct is not None and moved) \
            or (status == "completed" and seen_end and pct < REREAD_BELOW and moved)
        if not start:
            return ""
        if seen_end:
            conn.execute("UPDATE sync.kosync_position SET finished_at = NULL WHERE document IN"
                         " (SELECT kosync_md5 FROM catalog.edition_file WHERE edition_id = %s"
                         "  UNION SELECT document FROM catalog.document_alias WHERE edition_id = %s)", (edition, edition))
        conn.execute("INSERT INTO library.consumption (item_id, edition_id, state, started_on, started_precision,"
                     " progress_pct, progress_pages, progress_at, source)"
                     " VALUES (%s, %s, 'in_progress', %s, 'day', %s, %s, now(), 'kosync')",
                     (item, edition, today, pct100, pages))
        sync_status(conn, item)
        return "re-read started" if status == "completed" else "reading started"


def documents(conn):
    """Every synced document with the book it names (for the console)."""
    rows = conn.execute("""
        SELECT p.document, p.percentage, p.device, p.updated_at, p.first_seen_at, p.metadata,
               b.public_id::text AS item, b.title, b.how
          FROM sync.kosync_position p
          LEFT JOIN LATERAL (
                SELECT i.public_id, i.title, 'file' AS how FROM catalog.edition_file f
                  JOIN catalog.edition e ON e.id = f.edition_id JOIN catalog.media_item i ON i.id = e.work_id
                 WHERE f.kosync_md5 = p.document AND f.deleted_at IS NULL
                UNION ALL
                SELECT i.public_id, i.title, a.source FROM catalog.document_alias a
                  JOIN catalog.edition e ON e.id = a.edition_id JOIN catalog.media_item i ON i.id = e.work_id
                 WHERE a.document = p.document
                 LIMIT 1) b ON true
         ORDER BY p.updated_at DESC""").fetchall()
    return [dict(r, percentage=round(r["percentage"] * 100, 1)) for r in rows]


def link(conn, login, doc, item_pid):
    """The owner says which book a document is (a copy no stored file matches)."""
    if not re.fullmatch(r"[0-9a-f]{32}", doc or ""):
        raise BadDocument("not a document id")
    if book_for(conn, doc):
        raise BadDocument("that document already belongs to a book")
    ed = conn.execute("""SELECT e.id FROM catalog.media_item i JOIN catalog.edition e ON e.work_id = i.id
                          AND e.is_primary AND e.deleted_at IS NULL
                         WHERE i.public_id = %s AND i.deleted_at IS NULL AND i.kind = 'book'""", (item_pid,)).fetchone()
    if not ed:
        raise LookupError("no such book")
    with db.write_tx(conn, "owner", "console:" + (login or "unknown"), reason="link a Readest document"):
        conn.execute("INSERT INTO catalog.document_alias (document, edition_id, source) VALUES (%s, %s, 'owner')",
                     (doc, ed["id"]))

