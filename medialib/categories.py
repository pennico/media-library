"""Categories: two levels, a group (Fiction, Non-fiction) and the categories
inside it (migration 0013), and how labels map onto them.

Labels come from two places. **Your labels** (the genre column of the old
Excel list, via Yamtrack/CWA, or a Joplin note's ``genre:``) decide when they
can: "Novel | Sci-Fi" is Science fiction. **Provider subjects** (Google Books
categories such as "Fiction / Science Fiction / General", Open Library
subjects) fill in where your labels say only "Fiction" or nothing, or are too
broad ("Social Science"). Labels that are no category at all (Open Library
subjects such as "Brides") become plain tags, so nothing is lost.

    python -m medialib categorize [--apply] [--offline] [--out DIR]
"""
import collections
import datetime
import os
import re

from . import db

TREE = {
    "Fiction": ["Novel", "Historical fiction", "Crime & mystery", "Science fiction", "Fantasy", "Horror",
                "Short stories", "Drama & poetry", "Graphic novels", "Satire & humour"],
    "Non-fiction": ["History", "Biography & memoir", "Philosophy", "Psychology & self-help", "Science & nature",
                    "Economics & business", "Politics & society", "Essays & criticism", "Religion & spirituality",
                    "Art & culture", "Travel"],
}
GROUP_OF = {c: g for g, cs in TREE.items() for c in cs}

# Your labels -> a category, or a group (no finer category from this label)
LABELS = {
    "fiction": "Fiction", "narrativa": "Fiction", "skønlitteratur": "Fiction",
    "non-fiction": "Non-fiction", "nonfiction": "Non-fiction", "non fiction": "Non-fiction", "saggistica": "Non-fiction",
    "novel": "Novel", "novels": "Novel", "romanzo": "Novel", "roman": "Novel", "literary fiction": "Novel",
    "historical fiction": "Historical fiction", "historical novel": "Historical fiction",
    "mystery": "Crime & mystery", "detective": "Crime & mystery", "thriller": "Crime & mystery",
    "thriller - crime": "Crime & mystery", "crime": "Crime & mystery", "spy": "Crime & mystery", "noir": "Crime & mystery",
    "giallo": "Crime & mystery", "krimi": "Crime & mystery",
    "sci-fi": "Science fiction", "scifi": "Science fiction", "soft sci-fi": "Science fiction",
    "intimist sci-fi": "Science fiction", "dystopian": "Science fiction", "dystopia": "Science fiction",
    "fantasy": "Fantasy", "horror": "Horror", "short stories": "Short stories", "stories": "Short stories",
    "racconti": "Short stories", "play": "Drama & poetry", "plays": "Drama & poetry", "drama": "Drama & poetry",
    "poetry": "Drama & poetry", "poems": "Drama & poetry", "theatre": "Drama & poetry",
    "graphic novel": "Graphic novels", "comics": "Graphic novels", "satire": "Satire & humour",
    "humour": "Satire & humour", "humor": "Satire & humour",
    "history": "History", "biography": "Biography & memoir", "autobiography": "Biography & memoir",
    "memoir": "Biography & memoir", "memoirs": "Biography & memoir", "letter": "Biography & memoir",
    "letters": "Biography & memoir", "philosophy": "Philosophy", "philosophical": "Philosophy",
    "psychology": "Psychology & self-help", "self-help": "Psychology & self-help",
    "personal development": "Psychology & self-help", "self-actualization (psychology)": "Psychology & self-help",
    "productivity": "Psychology & self-help", "relationships": "Psychology & self-help",
    "science": "Science & nature", "nature": "Science & nature", "climate": "Science & nature",
    "economics": "Economics & business", "economy": "Economics & business", "business": "Economics & business",
    "finance": "Economics & business", "strategy": "Economics & business",
    "sociology/economics": "Economics & business", "management": "Economics & business",
    "politics": "Politics & society", "sociology": "Politics & society", "social science": "Politics & society",
    "media studies": "Politics & society", "ethnology": "Politics & society", "society": "Politics & society",
    "essay": "Essays & criticism", "essays": "Essays & criticism",
    "criticism and interpretation": "Essays & criticism", "literary criticism": "Essays & criticism",
    "religion": "Religion & spirituality", "spirituality": "Religion & spirituality",
    "art": "Art & culture", "music": "Art & culture", "travel": "Travel",
}
LABELS.update({c.lower(): c for c in GROUP_OF})
LABELS.update({g.lower(): g for g in TREE})
# broad labels: a provider subject in the same group says it better
WEAK = {"social science", "ethnology", "criticism and interpretation", "philosophical", "society", "letter", "strategy"}

# Provider subjects -> category, first match wins (lower case, "A / B / C" paths included)
SUBJECT_RULES = [
    (r"romance|love stories|classics|classic literature|fiction & literature|literary fiction"
     r"|narrativa letteraria|narrativa e letteratura|^romaner?$|skønlitteratur", "Novel"),
    (r"historical fiction|fiction\s*/\s*historical|narrativa storica|historiske romaner", "Historical fiction"),
    (r"gialli|thriller|krimi|kriminalromaner", "Crime & mystery"),
    (r"storia della scienza|history of science", "Science & nature"),
    (r"^storia\b|^historie\b", "History"),
    (r"biografi", "Biography & memoir"),
    (r"scienza|natur\b|videnskab", "Science & nature"),
    (r"science fiction|sci-?fi|dystopi|cyberpunk|space opera", "Science fiction"),
    (r"fantasy|magic|wizards", "Fantasy"),
    (r"horror|ghost stories|supernatural", "Horror"),
    (r"mystery|detective|crime fiction|thriller|suspense|espionage|spy stories|police procedural", "Crime & mystery"),
    (r"short stories|stories \(single author\)|anthologies", "Short stories"),
    (r"comics|graphic novels", "Graphic novels"),
    (r"humou?r|satire", "Satire & humour"),
    (r"\bdrama\b|poetry|plays|tragedies|comedies", "Drama & poetry"),
    (r"biography|autobiography|memoir|personal narratives|correspondence", "Biography & memoir"),
    (r"philosophy|ethics|stoic|metaphysics|existentialism", "Philosophy"),
    (r"psychology|self-help|self help|personal growth|motivational|happiness|mindfulness|relationships"
     r"|family & relationships|habit|success", "Psychology & self-help"),
    (r"business|economics|economic|finance|investing|management|entrepreneur|capitalism|money", "Economics & business"),
    (r"political|politics|social science|sociology|current affairs|law\b|democracy|propaganda|communism"
     r"|surveillance|technology & society|media", "Politics & society"),
    (r"history|historical|ancient|civilization|world war|medieval", "History"),
    (r"science|physics|biology|mathematics|nature|medical|climate|environment|energy|evolution|astronomy"
     r"|cosmology|epidemic", "Science & nature"),
    (r"literary criticism|essays|language arts|literary collections", "Essays & criticism"),
    (r"religion|spiritual|buddhism|christianity|bible|theology|body, mind", "Religion & spirituality"),
    (r"\bart\b|music|design|architecture|photography|games", "Art & culture"),
    (r"travel", "Travel"),
    (r"^fiction|fiction\s*/\s*(literary|general)|literary fiction|\bnovel", "Novel"),
]
_FICTION = re.compile(r"(?<!non)(?<!non-)(?<!non )\bfiction\b|\bnovels?\b|\bromanz[oi]\b|juvenile fiction|\bnarrativa\b"
                      r"|skønlitteratur|\bromaner\b")
_NONFICTION = re.compile(r"non-?fiction|nonfiction")
NOISE = "plain tag"


def label_category(label):
    return LABELS.get(" ".join(str(label or "").lower().split()))


def subject_categories(subjects):
    """Provider subjects -> (group or None, Counter of categories). `subjects`
    is a list of strings, or of lists (one per source): each source lists its
    main genre first, so its first subject counts most."""
    found = collections.Counter()
    fiction = nonfiction = 0
    lists = [x for x in (subjects or []) if isinstance(x, list)] or [list(subjects or [])]
    weighted = [(s, 4 if n == 0 else 2 if n == 1 else 1) for lst in lists for n, s in enumerate(lst)]
    for s, weight in weighted:
        low = " ".join(str(s).lower().split())
        if not low:
            continue
        if _NONFICTION.search(low):
            nonfiction += 1
        elif _FICTION.search(low):
            fiction += 1
        for rx, cat in SUBJECT_RULES:
            if re.search(rx, low):
                found[cat] += weight
                break
    if fiction != nonfiction:
        group = "Fiction" if fiction > nonfiction else "Non-fiction"
    else:                                       # no marker either way: the strongest category's group
        group = GROUP_OF[found.most_common(1)[0][0]] if found else None
    return group, found


def classify(labels, subjects=()):
    """Your labels + provider subjects -> (categories, noise labels, how, sure).

    categories: at most two names (categories, or a group alone), best first;
    noise: labels that are no category (kept as plain tags); sure: your own
    labels decided, so subjects need not be looked up."""
    mapped, noise = [], []
    for lab in labels:
        cat = label_category(lab)
        if cat:
            mapped.append((lab, cat))
        else:
            noise.append(lab)
    cats_only = [cat for _, cat in mapped]
    # an explicit group label decides; otherwise the categories' majority
    if "Fiction" in cats_only or "Novel" in cats_only:
        group = "Fiction"
    elif "Non-fiction" in cats_only:
        group = "Non-fiction"
    else:
        votes = collections.Counter(GROUP_OF[c] for c in cats_only if c in GROUP_OF)
        group = votes.most_common(1)[0][0] if votes else None
    s_group, s_found = subject_categories(subjects)
    how = []
    if group is None:
        group = s_group
        if group:
            how.append("group from the subjects")
    if group is None:
        return [], noise, "nothing to go on", False
    strong = [cat for lab, cat in mapped if GROUP_OF.get(cat) == group and lab.lower() not in WEAK]
    weak = [cat for lab, cat in mapped if GROUP_OF.get(cat) == group and lab.lower() in WEAK]
    if group == "Fiction" and len(set(strong)) > 1 and "Novel" in strong:
        strong = [c for c in strong if c != "Novel"]          # "Novel | Sci-Fi" is Science fiction
    from_subjects = [c for c, _ in s_found.most_common() if GROUP_OF[c] == group]
    if group == "Fiction" and len(from_subjects) > 1 and from_subjects[0] == "Novel" \
            and s_found[from_subjects[1]] * 2 >= s_found["Novel"]:
        from_subjects = from_subjects[1:]                     # a specific genre with real support over "literary"
    sure = bool(strong)
    if strong:
        cats = list(dict.fromkeys(strong))
        how.append("your labels")
    elif from_subjects:
        cats = from_subjects[:1]
        how.append("subjects")
    elif weak:
        cats = list(dict.fromkeys(weak))
        how.append("your broad label")
    elif group == "Fiction":
        cats = ["Novel"]
        how.append("fiction without a finer subject: Novel")
    else:
        cats = ["Non-fiction"]
        how.append("non-fiction without a finer subject")
    themes = [lab for lab, cat in mapped if cat in GROUP_OF and GROUP_OF[cat] != group]
    if themes:
        how.append("in a {} book, {} is a theme".format(group.lower(), ", ".join(themes)))
    return cats[:2], noise, "; ".join(how), sure


# ------------------------------------------------------------- database

def tree(conn):
    """{name: id} of the groups and categories (migration 0013)."""
    rows = conn.execute("SELECT id, name FROM library.tag WHERE kind = 'genre' AND deleted_at IS NULL"
                        " AND (parent_id IS NOT NULL OR id IN (SELECT parent_id FROM library.tag"
                        " WHERE parent_id IS NOT NULL))").fetchall()
    return {r["name"]: r["id"] for r in rows}


def _tag_id(conn, kind, name):
    row = conn.execute("SELECT id FROM library.tag WHERE kind = %s AND lower(catalog.f_unaccent(name)) ="
                       " lower(catalog.f_unaccent(%s)) AND deleted_at IS NULL", (kind, name)).fetchone()
    return row["id"] if row else conn.execute("INSERT INTO library.tag (kind, name) VALUES (%s, %s) RETURNING id",
                                              (kind, name[:100])).fetchone()["id"]


def assign(conn, item, categories, noise=(), source="categorize"):
    """Make the item's genres exactly `categories` (names in the tree); noise
    labels become plain tags. Owner-added genres are left alone. Caller holds
    the transaction. Returns True when something changed."""
    ids = tree(conn)
    want = [ids[c] for c in categories if c in ids]
    have = conn.execute("""SELECT it.tag_id, it.source, t.name FROM library.item_tag it JOIN library.tag t ON t.id = it.tag_id
                            WHERE it.item_id = %s AND t.kind = 'genre' ORDER BY it.position""", (item,)).fetchall()
    if any(h["source"] == "owner" for h in have):
        return False
    if [h["tag_id"] for h in have] == want and not noise:
        return False
    conn.execute("DELETE FROM library.item_tag it USING library.tag t WHERE t.id = it.tag_id AND it.item_id = %s"
                 " AND t.kind = 'genre'", (item,))
    for pos, tid in enumerate(want, 1):
        conn.execute("INSERT INTO library.item_tag (item_id, tag_id, position, source) VALUES (%s, %s, %s, %s)",
                     (item, tid, pos, source))
    taken = {r["tag_id"] for r in conn.execute("SELECT tag_id FROM library.item_tag WHERE item_id = %s", (item,))}
    n = conn.execute("SELECT count(*) AS n FROM library.item_tag it JOIN library.tag t ON t.id = it.tag_id"
                     " WHERE it.item_id = %s AND t.kind = 'tag'", (item,)).fetchone()["n"]
    for name in noise:
        tid = _tag_id(conn, "tag", name)
        if tid not in taken:
            n += 1
            conn.execute("INSERT INTO library.item_tag (item_id, tag_id, position, source) VALUES (%s, %s, %s, %s)",
                         (item, tid, n, source))
            taken.add(tid)
    return True


def retire_unused(conn):
    """Old flat genre labels no book uses any more leave the list."""
    rows = conn.execute("""UPDATE library.tag t SET deleted_at = now()
                            WHERE t.kind = 'genre' AND t.deleted_at IS NULL AND t.parent_id IS NULL
                              AND NOT EXISTS (SELECT 1 FROM library.tag c WHERE c.parent_id = t.id)
                              AND NOT EXISTS (SELECT 1 FROM library.item_tag it WHERE it.tag_id = t.id)
                        RETURNING t.name""").fetchall()
    return [r["name"] for r in rows]


def books(conn):
    return conn.execute("""
        SELECT i.id, i.title, i.locked,
               (SELECT array_agg(p.name::text ORDER BY c.position) FROM catalog.contribution c
                  JOIN catalog.person p ON p.id = c.person_id WHERE c.item_id = i.id AND c.role = 'author') AS authors,
               (SELECT array_agg(t.name::text ORDER BY it.position) FROM library.item_tag it
                  JOIN library.tag t ON t.id = it.tag_id WHERE it.item_id = i.id AND t.kind = 'genre') AS labels,
               (SELECT bool_or(it.source = 'owner') FROM library.item_tag it JOIN library.tag t ON t.id = it.tag_id
                 WHERE it.item_id = i.id AND t.kind = 'genre') AS owner_set,
               (SELECT x.value FROM catalog.external_id x JOIN catalog.edition e ON e.id = x.edition_id
                 WHERE e.work_id = i.id AND x.provider = 'isbn13' ORDER BY e.is_primary DESC LIMIT 1) AS isbn
          FROM catalog.media_item i JOIN library.entry en ON en.item_id = i.id AND en.deleted_at IS NULL
         WHERE i.kind = 'book' AND i.deleted_at IS NULL ORDER BY i.sort_title""").fetchall()


def subjects_online(fill):
    """A lookup of provider subjects for a book: Apple Books genres (main one
    first), Open Library subjects, Google Books categories when it has a key."""
    from .fill import providers as P

    def look(b):
        out = []
        for p in (fill.apple, fill.ol, fill.google):
            if not getattr(p, "available", True):
                continue
            cands = [fill.safe(p.by_isbn, b["isbn"])] if b["isbn"] else []
            cands = [c for c in cands if c] or fill.safe(p.search, b["title"], (b["authors"] or [None])[0], limit=3) or []
            for c in cands[:2]:
                if c and P.score(b["title"], b["authors"] or [], c) >= 0.82 and c.subjects:
                    out.append(list(c.subjects))
                    break
        return out
    return look


def run(conn, subjects_for=None, apply=False, out_dir="out", log=print):
    """Every book onto the tree. Returns the rows of the report."""
    rows, changed = [], 0
    for b in books(conn):
        labels = list(b["labels"] or [])
        if b["owner_set"] or b["locked"]:
            rows.append((b["title"], labels, labels, "kept: " + ("you set these" if b["owner_set"] else "locked")))
            continue
        cats, noise, how, sure = classify(labels)
        if not sure and subjects_for:
            subjects = subjects_for(b)
            if subjects:
                cats, noise, how, _ = classify(labels, subjects)
                how += " ({})".format(" / ".join("; ".join(lst[:3]) for lst in subjects))
        if not cats:
            rows.append((b["title"], labels, labels, how))
            continue
        if apply:
            with db.write_tx(conn, "categorize", "categorize", reason="category tree"):
                changed += assign(conn, b["id"], cats, noise)
        rows.append((b["title"], labels, cats + (["tags: " + ", ".join(noise)] if noise else []), how))
    retired = []
    if apply:
        with db.write_tx(conn, "categorize", "categorize", reason="unused genre labels"):
            retired = retire_unused(conn)
    path = report(rows, retired, out_dir, apply)
    log("{} books, {} changed{}; report: {}".format(len(rows), changed if apply else "(dry run)",
                                                     ", retired labels: " + ", ".join(retired) if retired else "", path))
    return rows


def report(rows, retired, out_dir, apply):
    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(out_dir, "categorize-{}-{}.md".format(ts, "apply" if apply else "dry-run"))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("# Categories ({})\n\n| Book | Before | After | Why |\n|---|---|---|---|\n".format(
            "applied" if apply else "dry run"))
        for title, before, after, how in rows:
            fh.write("| {} | {} | {} | {} |\n".format(title.replace("|", "/"), " · ".join(before) or "—",
                                                      " · ".join(after) or "—", how.replace("|", "/")))
        if retired:
            fh.write("\nUnused labels retired: {}\n".format(", ".join(retired)))
    return path
