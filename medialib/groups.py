"""Book series and film collections: items of one kind shown as one card, in an order (D49, D76, D77).

``catalog.item_group`` is the group (a book series, or a film collection: TMDB's or the owner's) and
``catalog.group_member`` the group an item is in and its place there. Every writer goes through here: TMDB
and the watch import (``screen.join_collection``) and the console (``web/writes.py``), inside the caller's
write transaction, so the provenance guard and the audit see who did it.

- An item is in at most one group. A member row with no group is the owner's "in no group": TMDB leaves
  such a film alone, as it leaves a film in a group the owner chose.
- Joining puts an item where its date belongs (the first published date, else the release date, else the
  edition's): before the first member dated later; undated ones go last.
- The order sent back from the console fills the places of the members it names; members it does not name
  (in the trash) keep theirs, so a book restored from the trash is back where it was.
- Deleting a group is soft and touches no member: its members are in no group while it is deleted, and an
  Undo brings it back exactly as it was.
"""
KINDS = ("book", "movie")
NAME_MAX = 300

# the date an item is ordered by in its group
DATED = """
SELECT i.id, i.kind, i.title, i.sort_title, i.deleted_at,
       coalesce(i.first_published, mv.release_date, ed.published_on) AS d
  FROM catalog.media_item i
  LEFT JOIN catalog.movie mv ON mv.item_id = i.id
  LEFT JOIN catalog.edition ed ON ed.work_id = i.id AND ed.is_primary AND ed.deleted_at IS NULL
"""


def _key(r):
    return (r["d"] is None, r["d"] or 0, r["sort_title"] or "", r["id"])


def by_date(conn, item_ids):
    """The items, oldest first (undated last, then by title)."""
    rows = conn.execute(DATED + " WHERE i.id = ANY(%s)", (list(item_ids),)).fetchall()
    return [r["id"] for r in sorted(rows, key=_key)]


def group(conn, gid):
    return conn.execute("SELECT * FROM catalog.item_group WHERE id = %s", (gid,)).fetchone()


def members(conn, gid):
    """The group's members in order, those in the trash among them: [{id, d, sort_title, deleted_at, position}]."""
    return conn.execute(DATED.replace("SELECT i.id,", "SELECT gm.position, i.id,")
                        + " JOIN catalog.group_member gm ON gm.item_id = i.id WHERE gm.group_id = %s"
                        " ORDER BY gm.position, i.id", (gid,)).fetchall()


def membership(conn, item):
    """(group id or None, the label of who put it there or None); (None, None) when it never was in one."""
    row = conn.execute("SELECT group_id, provenance #>> '{group_id,src}' AS src FROM catalog.group_member"
                       " WHERE item_id = %s", (item,)).fetchone()
    return (row["group_id"], row["src"]) if row else (None, None)


def live_group_of(conn, item):
    """The live group the item is in (a deleted group counts as none): the row, or None."""
    return conn.execute("""SELECT g.* FROM catalog.group_member gm JOIN catalog.item_group g ON g.id = gm.group_id
                            WHERE gm.item_id = %s AND g.deleted_at IS NULL""", (item,)).fetchone()


def find(conn, kind, name=None, tmdb=None):
    """A live group: TMDB's by its id, or the owner's of that kind and name. None when there is none."""
    if tmdb:
        return conn.execute("SELECT * FROM catalog.item_group WHERE tmdb_id = %s", (tmdb,)).fetchone()
    return conn.execute("SELECT * FROM catalog.item_group WHERE tmdb_id IS NULL AND media_kind = %s AND deleted_at IS NULL"
                        " AND lower(catalog.f_unaccent(name)) = lower(catalog.f_unaccent(%s))", (kind, name.strip())).fetchone()


def create(conn, kind, name, tmdb=None):
    return conn.execute("INSERT INTO catalog.item_group (name, media_kind, tmdb_id) VALUES (%s, %s, %s) RETURNING *",
                        (name.strip()[:NAME_MAX], kind, tmdb)).fetchone()


def _write(conn, gid, order):
    """The group's members are `order`, in that order (positions 1..n): rows moved, made or renumbered as needed.
    Positions are unique again at commit (the constraint is deferred)."""
    have = {r["item_id"]: r for r in conn.execute(
        "SELECT item_id, group_id, position FROM catalog.group_member WHERE item_id = ANY(%s)", (list(order),)).fetchall()}
    for pos, item in enumerate(order, 1):
        r = have.get(item)
        if r is None:
            conn.execute("INSERT INTO catalog.group_member (item_id, item_kind, group_id, position)"
                         " SELECT id, kind, %s, %s FROM catalog.media_item WHERE id = %s", (gid, pos, item))
        elif (r["group_id"], r["position"]) != (gid, pos):
            conn.execute("UPDATE catalog.group_member SET group_id = %s, position = %s WHERE item_id = %s", (gid, pos, item))


def place(conn, gid, items):
    """The items join the group (from none or another) where their dates belong. -> the ones that joined."""
    cur = members(conn, gid)
    order, dated = [r["id"] for r in cur], {r["id"]: r for r in cur}
    joining = [x for x in by_date(conn, items) if x not in dated]
    if not joining:
        return []
    for r in conn.execute(DATED + " WHERE i.id = ANY(%s)", (joining,)).fetchall():
        dated[r["id"]] = r
    for x in joining:
        d = dated[x]["d"]
        at = len(order) if d is None else next((k for k, y in enumerate(order) if dated[y]["d"] is not None and dated[y]["d"] > d),
                                                len(order))
        order.insert(at, x)
    _write(conn, gid, order)
    return joining


def set_order(conn, gid, items):
    """The order the console sends: the named items take the places of the named members, in the order named;
    members not named (in the trash) keep their place; named items not in it yet join after the rest (an Undo
    of a remove sends the order from before). Nothing leaves here."""
    cur = [r["id"] for r in members(conn, gid)]
    named, queue = set(items), list(items)
    order = [queue.pop(0) if x in named else x for x in cur]
    order += queue
    _write(conn, gid, order)


def leave(conn, items):
    """Out of their groups, into the owner's "in no group" (TMDB leaves them there). -> the items that left one."""
    left = set()
    for item in items:
        row = conn.execute("UPDATE catalog.group_member SET group_id = NULL, position = NULL"
                           " WHERE item_id = %s AND group_id IS NOT NULL RETURNING item_id", (item,)).fetchone()
        if row:
            left.add(item)
    return left


def live_order(conn, gid):
    """The public ids of the group's members the library shows (not in the trash), in order."""
    return [r["id"] for r in conn.execute(
        """SELECT i.public_id::text AS id FROM catalog.group_member gm
             JOIN catalog.media_item i ON i.id = gm.item_id AND i.deleted_at IS NULL
             JOIN library.entry e ON e.item_id = i.id AND e.deleted_at IS NULL
            WHERE gm.group_id = %s ORDER BY gm.position""", (gid,)).fetchall()]


def listing(conn):
    """Every live group, for the console's lists (Add to series…): id, name, kind, TMDB's or not, live members."""
    rows = conn.execute("""SELECT g.public_id::text AS id, g.name, g.media_kind AS kind, g.tmdb_id IS NOT NULL AS tmdb,
                                  (SELECT count(*) FROM catalog.group_member gm
                                     JOIN catalog.media_item i ON i.id = gm.item_id AND i.deleted_at IS NULL
                                     JOIN library.entry e ON e.item_id = i.id AND e.deleted_at IS NULL
                                    WHERE gm.group_id = g.id)::int AS n
                             FROM catalog.item_group g WHERE g.deleted_at IS NULL
                            ORDER BY g.media_kind, lower(g.name), g.id""").fetchall()
    return [dict(r) for r in rows]
