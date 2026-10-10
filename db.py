"""Data layer: SQLite (local dev) or Postgres (production via DATABASE_URL).

Uses stdlib sqlite3 by default. If DATABASE_URL is set, uses psycopg2
with Postgres (Neon). No ORM.
"""
import json
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ai_grader.db")
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_POSTGRES = bool(DATABASE_URL)

_psycopg2 = None
if USE_POSTGRES:
    try:
        import psycopg2
        import psycopg2.extras
        _psycopg2 = psycopg2
    except ImportError:
        # Critical: if DATABASE_URL is set but driver missing, fail loudly.
        # Silent SQLite fallback on ephemeral Render disk = data loss.
        raise RuntimeError(
            "DATABASE_URL is set but psycopg2 is not installed. "
            "Add psycopg2-binary to requirements.txt."
        )


def _connect():
    if USE_POSTGRES:
        conn = _psycopg2.connect(DATABASE_URL)
        return conn
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _q(sql):
    """Convert ? placeholders to %s for Postgres.

    Handles % in literals (escapes to %%) and ? inside quoted strings
    (leaves them alone). Without this, psycopg2 misinterprets % as
    format markers and ? in literals gets corrupted.
    """
    if not USE_POSTGRES:
        return sql
    out = []
    in_str = False
    i = 0
    while i < len(sql):
        ch = sql[i]
        if ch == "'":
            # Handle escaped quotes ('')
            if in_str and i + 1 < len(sql) and sql[i + 1] == "'":
                out.append("''")
                i += 2
                continue
            in_str = not in_str
            out.append(ch)
        elif ch == "%" and not in_str:
            out.append("%%")
        elif ch == "?" and not in_str:
            out.append("%s")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _rows_to_dicts(rows, cursor=None):
    """Normalize rows to list of dicts for both drivers."""
    if not rows:
        return []
    if USE_POSTGRES:
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, r)) for r in rows]
    return [dict(r) for r in rows]


def _one_to_dict(row, cursor=None):
    if row is None:
        return None
    if USE_POSTGRES:
        cols = [d[0] for d in cursor.description]
        return dict(zip(cols, row))
    return dict(row)


def _id_col():
    return "SERIAL PRIMARY KEY" if USE_POSTGRES else "INTEGER PRIMARY KEY AUTOINCREMENT"


def _table_cols(conn, table):
    """Return list of column names for a table (both drivers)."""
    cur = conn.cursor()
    if USE_POSTGRES:
        cur.execute(_q("SELECT column_name FROM information_schema.columns WHERE table_name = ?"), (table,))
        return [r[0] for r in cur.fetchall()]
    return [r[1] for r in cur.execute("PRAGMA table_info(%s)" % table).fetchall()]


def init_db():
    conn = _connect()
    cur = conn.cursor()
    idcol = _id_col()
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS classes (
            id {idcol},
            name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            owner TEXT NOT NULL DEFAULT 'private'
        )
    """)
    # Migration: data-isolation owner column (public test account, 2026-10-10).
    # Existing classes belong to the owner ('private'); the public test role
    # only sees rows with owner='public'.
    cols = _table_cols(conn, "classes")
    if "owner" not in cols:
        cur.execute("ALTER TABLE classes ADD COLUMN owner TEXT NOT NULL DEFAULT 'private'")
    # Rate-limit counters for restricted roles (public test account).
    # One row per (role, op, day); day is YYYY-MM-DD in server local time.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS rate_limit_counters (
            role TEXT NOT NULL,
            op TEXT NOT NULL,
            day TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (role, op, day)
        )
    """)
    # Image ownership for data-partition authorization on /uploads/<filename>.
    # Maps the stored filename to the data partition ('private' or 'public')
    # that uploaded it, so the file-serving route can enforce isolation.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS image_owners (
            filename TEXT PRIMARY KEY,
            owner TEXT NOT NULL DEFAULT 'private',
            created REAL NOT NULL
        )
    """)
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS assignments (
            id {idcol},
            class_id INTEGER NOT NULL REFERENCES classes(id),
            name TEXT NOT NULL,
            rubric_text TEXT DEFAULT '',
            rubric_json TEXT DEFAULT '[]',
            reference_images TEXT DEFAULT '[]',
            created_at TEXT NOT NULL
        )
    """)
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS grades (
            id {idcol},
            assignment_id INTEGER NOT NULL REFERENCES assignments(id),
            student_name TEXT NOT NULL,
            questions_json TEXT NOT NULL,
            total REAL NOT NULL,
            max_total REAL NOT NULL,
            overall_feedback TEXT DEFAULT '',
            created_at TEXT NOT NULL
        )
    """)
    # Migration: rubric example images library (added after initial release).
    cols = _table_cols(conn, "assignments")
    if "rubric_example_images" not in cols:
        cur.execute("ALTER TABLE assignments ADD COLUMN rubric_example_images TEXT DEFAULT '[]'")
    # Migration: saved AP standard comparison (added after initial release).
    if "ap_comparison" not in cols:
        cur.execute("ALTER TABLE assignments ADD COLUMN ap_comparison TEXT DEFAULT ''")
    # Migration: student paper photos linked to each grade.
    gcols = _table_cols(conn, "grades")
    if "images" not in gcols:
        cur.execute("ALTER TABLE grades ADD COLUMN images TEXT DEFAULT '[]'")
    # API usage log: every OpenAI call, for owner cost visibility.
    # The `role` column tracks which login role made the call so the owner can
    # monitor the public demo account separately (added 2026-10-10).
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS api_usage (
            id {idcol},
            endpoint TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            prompt_tokens INTEGER NOT NULL DEFAULT 0,
            completion_tokens INTEGER NOT NULL DEFAULT 0,
            created REAL NOT NULL,
            role TEXT NOT NULL DEFAULT 'private'
        )
    """)
    ucols = _table_cols(conn, "api_usage")
    if "role" not in ucols:
        cur.execute("ALTER TABLE api_usage ADD COLUMN role TEXT NOT NULL DEFAULT 'private'")
    # Rubric generation jobs: DB-backed so jobs survive multi-worker gunicorn
    # and Render restarts (in-memory dict lost jobs with --workers 2).
    # assignment_id links the job to its assignment for data-partition
    # authorization on the poll endpoint (added 2026-10-10).
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS rubric_jobs (
            job_id TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'running',
            total INTEGER NOT NULL DEFAULT 0,
            done_json TEXT NOT NULL DEFAULT '[]',
            items_json TEXT NOT NULL DEFAULT '[]',
            error TEXT DEFAULT '',
            created REAL NOT NULL,
            updated REAL NOT NULL,
            assignment_id INTEGER NOT NULL DEFAULT 0
        )
    """)
    jcols = _table_cols(conn, "rubric_jobs")
    if "assignment_id" not in jcols:
        cur.execute("ALTER TABLE rubric_jobs ADD COLUMN assignment_id INTEGER NOT NULL DEFAULT 0")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_api_usage_created ON api_usage(created)")
    # Uploaded images: stored in DB when USE_POSTGRES (Render disk is ephemeral).
    # Local dev keeps using the filesystem.
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS images (
            name TEXT PRIMARY KEY,
            data {"BYTEA" if USE_POSTGRES else "BLOB"} NOT NULL,
            mime_type TEXT NOT NULL DEFAULT 'image/jpeg',
            created REAL NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def save_image(name, data, mime_type="image/jpeg"):
    """Store an uploaded image in the DB (Postgres mode)."""
    conn = _connect()
    cur = conn.cursor()
    if USE_POSTGRES:
        cur.execute(_q(
            "INSERT INTO images (name, data, mime_type, created) VALUES (?, ?, ?, ?)"
            " ON CONFLICT (name) DO UPDATE SET data=EXCLUDED.data,"
            " mime_type=EXCLUDED.mime_type, created=EXCLUDED.created"),
            (name, data, mime_type, time.time()))
    else:
        cur.execute(
            "INSERT OR REPLACE INTO images (name, data, mime_type, created)"
            " VALUES (?, ?, ?, ?)",
            (name, data, mime_type, time.time()))
    conn.commit()
    conn.close()


def get_image(name):
    """Return (data_bytes, mime_type) or (None, None)."""
    conn = _connect()
    cur = conn.cursor()
    cur.execute(_q("SELECT data, mime_type FROM images WHERE name = ?"), (name,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None, None
    if USE_POSTGRES:
        data = bytes(row[0]) if row[0] is not None else None
        return data, row[1]
    return row[0], row[1]


def _normalize_image_names(names):
    """Normalize stored image entries to bare filenames, deduplicated.

    Both the "db:" form and full paths go through os.path.basename: a
    "db:../../x" entry must not survive as a traversal primitive (dual
    review 2026-10-10).
    """
    seen = []
    for p in names:
        if isinstance(p, str) and p.startswith("db:"):
            raw = p[3:]
        else:
            raw = p or ""
        base = os.path.basename(raw)
        if base and base not in (".", "..") and base not in seen:
            seen.append(base)
    return seen


_UPLOADS_URL_RE = re.compile(r"/uploads/([A-Za-z0-9_.\-]+)")


def _diagram_names_in_rubric(rubric_json):
    """Bare filenames referenced as /uploads/<name> inside stored rubric JSON.

    Generated diagrams (_process_graph) are recorded in the rubric items'
    `graph` fields, not in the image-list columns — without this scan,
    assignment deletion would never purge them (storage leak).
    """
    if not isinstance(rubric_json, str):
        return []
    names = []
    for m in _UPLOADS_URL_RE.finditer(rubric_json):
        base = os.path.basename(m.group(1))
        if base and base not in (".", "..") and base not in names:
            names.append(base)
    return names


def _assignment_image_names_cur(cur, assignment_id):
    """Image names referenced by an assignment (cursor-based).

    For use inside a serialized transaction: the caller holds the
    assignment-images lock. Covers reference images, rubric example images,
    generated-diagram URLs inside the rubric JSON, and every grade's paper
    photos.
    """
    names = []
    cur.execute(_q("SELECT reference_images, rubric_example_images, rubric_json"
                   " FROM assignments WHERE id = ?"), (assignment_id,))
    row = cur.fetchone()
    if row:
        for col in (row[0], row[1]):
            try:
                names.extend(json.loads(col or "[]"))
            except (TypeError, ValueError):
                pass
        names.extend(_diagram_names_in_rubric(row[2]))
    cur.execute(_q("SELECT images FROM grades WHERE assignment_id = ?"),
                (assignment_id,))
    for (col,) in cur.fetchall():
        try:
            names.extend(json.loads(col or "[]"))
        except (TypeError, ValueError):
            pass
    return _normalize_image_names(names)


def delete_image(name):
    """Delete an image's bytes and its ownership record (storage hygiene)."""
    conn = _connect()
    cur = conn.cursor()
    cur.execute(_q("DELETE FROM images WHERE name = ?"), (name,))
    cur.execute(_q("DELETE FROM image_owners WHERE filename = ?"), (name,))
    conn.commit()
    conn.close()


def get_assignment_image_names(assignment_id):
    """All stored image filenames referenced by an assignment.

    Covers reference images, rubric example images, and every grade's paper
    photos — used to purge bytes when the assignment is deleted.
    """
    conn = _connect()
    cur = conn.cursor()
    try:
        return _assignment_image_names_cur(cur, assignment_id)
    finally:
        conn.close()


def delete_assignment_returning_images(assignment_id):
    """Delete an assignment (+ its grades) and return its image names.

    The name gathering and the row deletes run in ONE serialized transaction
    under the same assignment-images lock as try_append_image_paths, so a
    concurrent upload cannot slip new bytes in between the snapshot and the
    delete (which would orphan them). Callers purge the returned names' bytes
    after this returns (after commit).
    """
    with serialized_mutation(f"assignment-images:{assignment_id}") as conn:
        cur = conn.cursor()
        names = _assignment_image_names_cur(cur, assignment_id)
        cur.execute(_q("DELETE FROM grades WHERE assignment_id = ?"),
                    (assignment_id,))
        cur.execute(_q("DELETE FROM assignments WHERE id = ?"), (assignment_id,))
        return names


def image_referenced_elsewhere(name, exclude_assignment_id=None):
    """True if image `name` is still referenced outside one assignment.

    Scans every other assignment's reference/example image lists and every
    other assignment's grades. Used during assignment deletion so a shared
    image (legacy name collision, or a copy feature) is never destroyed while
    another assignment still needs it. `name` is a bare normalized filename.
    """
    def _bases(col):
        try:
            entries = json.loads(col or "[]")
        except (TypeError, ValueError):
            return
        for e in entries:
            if isinstance(e, str) and e.startswith("db:"):
                raw = e[3:]
            else:
                raw = e or ""
            yield os.path.basename(raw)

    conn = _connect()
    cur = conn.cursor()
    cur.execute(_q("SELECT id, reference_images, rubric_example_images, rubric_json"
                   " FROM assignments"))
    for aid, ref_col, ex_col, rubric_col in cur.fetchall():
        if exclude_assignment_id is not None and aid == exclude_assignment_id:
            continue
        for col in (ref_col, ex_col):
            if name in _bases(col):
                conn.close()
                return True
        if name in _diagram_names_in_rubric(rubric_col):
            conn.close()
            return True
    if exclude_assignment_id is None:
        cur.execute(_q("SELECT images FROM grades"))
        grade_rows = cur.fetchall()
    else:
        cur.execute(_q("SELECT images FROM grades WHERE assignment_id != ?"),
                    (exclude_assignment_id,))
        grade_rows = cur.fetchall()
    for (col,) in grade_rows:
        if name in _bases(col):
            conn.close()
            return True
    conn.close()
    return False


# Thread-local reentrancy flag for serialized_mutation (see below).
_serialized_state = threading.local()


@contextmanager
def serialized_mutation(lock_key):
    """Run a check-then-mutate sequence as one atomic unit.

    SQLite: BEGIN IMMEDIATE takes a reserved write lock on the whole DB file,
    so concurrent writers serialize. Postgres: a transaction-scoped advisory
    lock keyed by `lock_key` serializes mutations for the same owner or
    assignment across processes. Use for every capped mutation so a parallel
    burst of requests cannot overshoot the demo account's storage caps
    (count-then-insert races, dual review 2026-10-10).

    Not reentrant: nesting would self-deadlock on Postgres (the advisory lock
    waits inside a single thread, invisible to deadlock detection) and fail
    with "database is locked" on SQLite. Fail loudly instead.
    """
    if getattr(_serialized_state, "active", False):
        raise RuntimeError("serialized_mutation is not reentrant")
    # Set the flag before _connect(): if connecting raises, the finally below
    # still clears it — otherwise one transient DB failure would permanently
    # poison this worker thread (dual review 2026-10-10).
    _serialized_state.active = True
    conn = None
    try:
        conn = _connect()
        if USE_POSTGRES:
            cur = conn.cursor()
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (lock_key,))
        else:
            conn.isolation_level = None  # autocommit; we manage BEGIN/COMMIT
            conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        if conn is not None:
            conn.rollback()
        raise
    finally:
        try:
            if conn is not None:
                conn.close()
        finally:
            _serialized_state.active = False


def create_class_capped(name, owner, cap):
    """Create a class for `owner` if under `cap`; return id, or None if capped.

    The count check and the insert run atomically (see serialized_mutation),
    so concurrent requests cannot overshoot the cap.
    """
    if owner not in ("private", "public"):
        raise ValueError(f"Invalid owner: {owner!r}")
    with serialized_mutation(f"classes:{owner}") as conn:
        cur = conn.cursor()
        cur.execute(_q("SELECT COUNT(*) FROM classes WHERE owner = ?"), (owner,))
        if cur.fetchone()[0] >= cap:
            return None
        return _insert_returning_id(
            cur, "INSERT INTO classes (name, created_at, owner) VALUES (?, ?, ?)",
            (name, now(), owner))


def create_assignment_capped(class_id, name, owner, cap):
    """Create an assignment if `owner` is under `cap`; None if capped.

    Raises ValueError on duplicate name in the class (same contract as
    create_assignment). Count, duplicate check, and insert are atomic.
    """
    with serialized_mutation(f"assignments:{owner}") as conn:
        cur = conn.cursor()
        cur.execute(_q("SELECT COUNT(*) FROM assignments a"
                       " JOIN classes c ON a.class_id = c.id"
                       " WHERE c.owner = ?"), (owner,))
        if cur.fetchone()[0] >= cap:
            return None
        cur.execute(
            _q("SELECT id FROM assignments WHERE class_id = ? AND name = ?"),
            (class_id, name))
        if cur.fetchone():
            raise ValueError(f"Assignment '{name}' already exists in this class.")
        return _insert_returning_id(
            cur,
            "INSERT INTO assignments (class_id, name, created_at) VALUES (?, ?, ?)",
            (class_id, name, now()))


_IMAGE_LIST_COLUMNS = ("reference_images", "rubric_example_images")


def try_append_image_paths(assignment_id, column, paths, cap=None):
    """Atomically append `paths` to an assignment's image list.

    `column` must be one of the image-list columns. When `cap` is set, the
    append only happens if len(current) + len(paths) <= cap; otherwise the
    stored list is untouched and False is returned. The read-check-update
    runs atomically (see serialized_mutation) so a parallel upload burst
    cannot overshoot the cap. Returns True on success.
    """
    if column not in _IMAGE_LIST_COLUMNS:
        raise ValueError(f"Not an image-list column: {column!r}")
    # Same assignment-images lock as try_remove_image_path and
    # delete_assignment_returning_images: appends, removes, and deletes are
    # mutually serialized so no update is lost and no upload slips past a
    # delete snapshot.
    with serialized_mutation(f"assignment-images:{assignment_id}") as conn:
        cur = conn.cursor()
        cur.execute(_q(f"SELECT {column} FROM assignments WHERE id = ?"),
                    (assignment_id,))
        row = cur.fetchone()
        if not row:
            return False
        try:
            current = json.loads(row[0] or "[]")
        except (TypeError, ValueError):
            current = []
        if cap is not None and len(current) + len(list(paths)) > cap:
            return False
        merged = list(current) + list(paths)
        cur.execute(_q(f"UPDATE assignments SET {column} = ? WHERE id = ?"),
                    (json.dumps(merged), assignment_id))
        return True


def try_remove_image_path(assignment_id, column, name):
    """Atomically remove list entries matching `name`; return removed paths.

    `name` is a bare filename. An entry matches when it equals `name`, equals
    "db:{name}", or its basename equals `name` (covers full-path forms).
    Runs under the same assignment-images lock as try_append_image_paths, so
    a concurrent append cannot be silently lost (no read-modify-write race).
    Returns the removed stored paths so the caller can purge their bytes
    after commit.
    """
    if column not in _IMAGE_LIST_COLUMNS:
        raise ValueError(f"Not an image-list column: {column!r}")

    def _matches(p):
        if not isinstance(p, str):
            return False
        if p == name or p == f"db:{name}":
            return True
        raw = p[3:] if p.startswith("db:") else p
        return os.path.basename(raw) == name

    with serialized_mutation(f"assignment-images:{assignment_id}") as conn:
        cur = conn.cursor()
        cur.execute(_q(f"SELECT {column} FROM assignments WHERE id = ?"),
                    (assignment_id,))
        row = cur.fetchone()
        if not row:
            return []
        try:
            current = json.loads(row[0] or "[]")
        except (TypeError, ValueError):
            current = []
        removed = [p for p in current if _matches(p)]
        kept = [p for p in current if not _matches(p)]
        cur.execute(_q(f"UPDATE assignments SET {column} = ? WHERE id = ?"),
                    (json.dumps(kept), assignment_id))
        return removed


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------- Classes ----------
def _insert_returning_id(cur, sql, params):
    """INSERT and return the new row id (both drivers)."""
    if USE_POSTGRES:
        cur.execute(_q(sql + " RETURNING id"), params)
        return cur.fetchone()[0]
    cur.execute(sql, params)
    return cur.lastrowid


def create_class(name, owner="private"):
    """Create a class owned by `owner` ('private' for the owner, 'public' for
    the public test role). Data is isolated by owner."""
    if owner not in ("private", "public"):
        raise ValueError(f"Invalid owner: {owner!r}")
    conn = _connect()
    cur = conn.cursor()
    cid = _insert_returning_id(cur,
        "INSERT INTO classes (name, created_at, owner) VALUES (?, ?, ?)",
        (name, now(), owner))
    conn.commit()
    conn.close()
    return cid


def list_classes(owner="private"):
    """List classes visible to `owner`. The public test role only sees its own."""
    conn = _connect()
    cur = conn.cursor()
    cur.execute(_q("SELECT * FROM classes WHERE owner = ? ORDER BY id DESC"),
                (owner,))
    rows = _rows_to_dicts(cur.fetchall(), cur)
    conn.close()
    return rows


def count_classes(owner="private"):
    """Number of classes in one data partition (for demo data-volume caps)."""
    conn = _connect()
    cur = conn.cursor()
    cur.execute(_q("SELECT COUNT(*) FROM classes WHERE owner = ?"), (owner,))
    n = cur.fetchone()[0]
    conn.close()
    return n


def count_assignments_for_owner(owner="private"):
    """Number of assignments in one data partition (demo data-volume caps)."""
    conn = _connect()
    cur = conn.cursor()
    cur.execute(_q("SELECT COUNT(*) FROM assignments a"
                   " JOIN classes c ON a.class_id = c.id"
                   " WHERE c.owner = ?"), (owner,))
    n = cur.fetchone()[0]
    conn.close()
    return n


def get_class(class_id):
    conn = _connect()
    cur = conn.cursor()
    cur.execute(_q("SELECT * FROM classes WHERE id = ?"), (class_id,))
    row = _one_to_dict(cur.fetchone(), cur)
    conn.close()
    return row


# ---------- Grading assignments ----------
def create_assignment(class_id, name):
    conn = _connect()
    cur = conn.cursor()
    # Reject duplicate names within the same class (user directive 2026-10-07).
    cur.execute(
        _q("SELECT id FROM assignments WHERE class_id = ? AND name = ?"),
        (class_id, name),
    )
    if cur.fetchone():
        conn.close()
        raise ValueError(f"Assignment '{name}' already exists in this class.")
    aid = _insert_returning_id(cur,
        "INSERT INTO assignments (class_id, name, created_at) VALUES (?, ?, ?)",
        (class_id, name, now()))
    conn.commit()
    conn.close()
    return aid


def list_assignments(class_id):
    conn = _connect()
    cur = conn.cursor()
    cur.execute(_q("SELECT * FROM assignments WHERE class_id = ? ORDER BY id DESC"), (class_id,))
    rows = _rows_to_dicts(cur.fetchall(), cur)
    conn.close()
    return rows


def get_assignment(assignment_id):
    conn = _connect()
    cur = conn.cursor()
    cur.execute(_q("SELECT * FROM assignments WHERE id = ?"), (assignment_id,))
    row = _one_to_dict(cur.fetchone(), cur)
    conn.close()
    return row


def save_rubric(assignment_id, rubric_text, rubric_items):
    """Save the structured rubric (JSON) plus the auto-generated plain-text version."""
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        _q("UPDATE assignments SET rubric_text = ?, rubric_json = ? WHERE id = ?"),
        (rubric_text, json.dumps(rubric_items, ensure_ascii=False), assignment_id),
    )
    conn.commit()
    conn.close()


def save_reference_images(assignment_id, paths):
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        _q("UPDATE assignments SET reference_images = ? WHERE id = ?"),
        (json.dumps(paths, ensure_ascii=False), assignment_id),
    )
    conn.commit()
    conn.close()


# ---------- Grades ----------
def save_grade(assignment_id, student_name, questions, total, max_total, overall_feedback, images=None):
    conn = _connect()
    cur = conn.cursor()
    gid = _insert_returning_id(cur,
        """INSERT INTO grades
           (assignment_id, student_name, questions_json, total, max_total, overall_feedback, images, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            assignment_id,
            student_name,
            json.dumps(questions, ensure_ascii=False),
            total,
            max_total,
            overall_feedback,
            json.dumps(images or [], ensure_ascii=False),
            now(),
        ))
    conn.commit()
    conn.close()
    return gid


def list_grades(assignment_id):
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        _q("SELECT * FROM grades WHERE assignment_id = ? ORDER BY total DESC, id ASC"),
        (assignment_id,),
    )
    rows = _rows_to_dicts(cur.fetchall(), cur)
    conn.close()
    return rows


def get_grade(grade_id):
    conn = _connect()
    cur = conn.cursor()
    cur.execute(_q("SELECT * FROM grades WHERE id = ?"), (grade_id,))
    row = _one_to_dict(cur.fetchone(), cur)
    conn.close()
    return row


def get_rubric_example_images(assignment_id):
    a = get_assignment(assignment_id)
    if not a:
        return []
    try:
        return json.loads(a["rubric_example_images"] or "[]")
    except (json.JSONDecodeError, TypeError):
        return []


def save_rubric_example_images(assignment_id, paths):
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        _q("UPDATE assignments SET rubric_example_images = ? WHERE id = ?"),
        (json.dumps(paths, ensure_ascii=False), assignment_id),
    )
    conn.commit()
    conn.close()


def save_ap_comparison(assignment_id, comparison):
    """Persist the AP standard comparison so suggestions survive page refresh."""
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        _q("UPDATE assignments SET ap_comparison = ? WHERE id = ?"),
        (json.dumps(comparison, ensure_ascii=False), assignment_id),
    )
    conn.commit()
    conn.close()


def get_ap_comparison(assignment_id):
    """Return the saved AP comparison dict, or None."""
    a = get_assignment(assignment_id)
    if not a or not a.get("ap_comparison"):
        return None
    try:
        return json.loads(a["ap_comparison"])
    except (json.JSONDecodeError, TypeError):
        return None


def remove_ap_question(assignment_id, q_label):
    """Remove one question's AP suggestion (after accept or dismiss)."""
    comp = get_ap_comparison(assignment_id)
    if not comp:
        return
    comp["questions"] = [
        q for q in (comp.get("questions") or []) if q.get("q") != q_label
    ]
    save_ap_comparison(assignment_id, comp)


def delete_assignment(assignment_id):
    """Delete an assignment and all of its saved grades."""
    conn = _connect()
    cur = conn.cursor()
    cur.execute(_q("DELETE FROM grades WHERE assignment_id = ?"), (assignment_id,))
    cur.execute(_q("DELETE FROM assignments WHERE id = ?"), (assignment_id,))
    conn.commit()
    conn.close()


# ---------- Rubric generation jobs (DB-backed) ----------
# Jobs were kept in an in-memory dict, which broke under gunicorn --workers 2
# (each worker has its own memory: job created in worker A, poll hits worker B
# -> "Job not found") and on Render restarts. DB storage fixes both.

JOB_STALE_SECONDS = 900  # a "running" job with no update for 15 min is dead


def create_rubric_job(job_id, assignment_id=0):
    conn = _connect()
    cur = conn.cursor()
    now_ts = time.time()
    if USE_POSTGRES:
        cur.execute(_q(
            "INSERT INTO rubric_jobs (job_id, status, total, done_json, items_json, error, created, updated, assignment_id)"
            " VALUES (?, 'running', 0, '[]', '[]', '', ?, ?, ?)"
            " ON CONFLICT (job_id) DO UPDATE SET status='running', total=0,"
            " done_json='[]', items_json='[]', error='', created=EXCLUDED.created, updated=EXCLUDED.updated,"
            " assignment_id=EXCLUDED.assignment_id"),
            (job_id, now_ts, now_ts, assignment_id),
        )
    else:
        cur.execute(
            "INSERT OR REPLACE INTO rubric_jobs (job_id, status, total, done_json, items_json, error, created, updated, assignment_id)"
            " VALUES (?, 'running', 0, '[]', '[]', '', ?, ?, ?)",
            (job_id, now_ts, now_ts, assignment_id),
        )
    conn.commit()
    conn.close()


def get_rubric_job(job_id):
    conn = _connect()
    cur = conn.cursor()
    cur.execute(_q("SELECT * FROM rubric_jobs WHERE job_id = ?"), (job_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return None
    d = _one_to_dict(row, cur)
    conn.close()
    return {
        "job_id": d["job_id"],
        "status": d["status"],
        "total": d["total"],
        "done": json.loads(d["done_json"] or "[]"),
        "items": json.loads(d["items_json"] or "[]"),
        "error": d["error"] or "",
        "created": d["created"],
        "updated": d["updated"],
        "assignment_id": d.get("assignment_id", 0),
    }


def _touch_job(conn, job_id):
    cur = conn.cursor()
    cur.execute(_q("UPDATE rubric_jobs SET updated = ? WHERE job_id = ?"), (time.time(), job_id))


def set_rubric_job_total(job_id, total):
    conn = _connect()
    cur = conn.cursor()
    cur.execute(_q("UPDATE rubric_jobs SET total = ? WHERE job_id = ?"), (total, job_id))
    _touch_job(conn, job_id)
    conn.commit()
    conn.close()


def append_rubric_job_item(job_id, item):
    conn = _connect()
    cur = conn.cursor()
    cur.execute(_q("SELECT done_json FROM rubric_jobs WHERE job_id = ?"), (job_id,))
    row = cur.fetchone()
    if row:
        d = _one_to_dict(row, cur)
        done = json.loads(d["done_json"] or "[]")
        done.append(item)
        cur.execute(_q("UPDATE rubric_jobs SET done_json = ? WHERE job_id = ?"), (json.dumps(done), job_id))
        _touch_job(conn, job_id)
        conn.commit()
    conn.close()


def finish_rubric_job(job_id, items):
    conn = _connect()
    cur = conn.cursor()
    payload = json.dumps(items)
    cur.execute(
        _q("UPDATE rubric_jobs SET status = 'done', items_json = ?, done_json = ?, total = ? WHERE job_id = ?"),
        (payload, payload, len(items), job_id),
    )
    _touch_job(conn, job_id)
    conn.commit()
    conn.close()


def fail_rubric_job(job_id, error):
    conn = _connect()
    cur = conn.cursor()
    cur.execute(_q("UPDATE rubric_jobs SET status = 'error', error = ? WHERE job_id = ?"), (str(error), job_id))
    _touch_job(conn, job_id)
    conn.commit()
    conn.close()


def cleanup_old_rubric_jobs(max_age_seconds=1800):
    conn = _connect()
    cur = conn.cursor()
    cur.execute(_q("DELETE FROM rubric_jobs WHERE created < ?"), (time.time() - max_age_seconds,))
    conn.commit()
    conn.close()


# ---------- API usage tracking ----------
# Every OpenAI call is logged so the owner can see (and cap) spending when
# sharing the deployment with alpha testers.

def log_api_usage(endpoint, model, prompt_tokens, completion_tokens, role="private"):
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        _q("INSERT INTO api_usage (endpoint, model, prompt_tokens, completion_tokens, created, role)"
        " VALUES (?, ?, ?, ?, ?, ?)"),
        (endpoint, model, prompt_tokens or 0, completion_tokens or 0, time.time(), role),
    )
    conn.commit()
    conn.close()


def get_api_usage_summary(since_ts=0):
    """Returns {calls, prompt_tokens, completion_tokens, by_model: {...}}."""
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        _q("SELECT COUNT(*), COALESCE(SUM(prompt_tokens),0), COALESCE(SUM(completion_tokens),0)"
        " FROM api_usage WHERE created >= ?"), (since_ts,))
    row = cur.fetchone()
    by_model = {}
    cur.execute(
        _q("SELECT model, COUNT(*), COALESCE(SUM(prompt_tokens),0),"
        " COALESCE(SUM(completion_tokens),0) FROM api_usage"
        " WHERE created >= ? GROUP BY model"), (since_ts,))
    for m, c, pt, ct in cur.fetchall():
        by_model[m or "?"] = {"calls": c, "tokens": (pt or 0) + (ct or 0)}
    conn.close()
    return {"calls": row[0], "prompt_tokens": row[1], "completion_tokens": row[2],
            "by_model": by_model}


def get_api_usage_summary_for_role(role, day=None):
    """Token usage summary for one role on one day (YYYY-MM-DD, server local).

    Used for the public demo account's daily token budget and the admin
    monitoring page. `day=None` means today.
    """
    if day is None:
        day = _rate_limit_day()
    # Day boundaries in epoch seconds (server local time).
    start = time.mktime(time.strptime(day, "%Y-%m-%d"))
    end = start + 86400
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        _q("SELECT COUNT(*), COALESCE(SUM(prompt_tokens),0),"
           " COALESCE(SUM(completion_tokens),0) FROM api_usage"
           " WHERE role = ? AND created >= ? AND created < ?"),
        (role, start, end))
    row = cur.fetchone()
    by_endpoint = {}
    cur.execute(
        _q("SELECT endpoint, COUNT(*), COALESCE(SUM(prompt_tokens),0),"
           " COALESCE(SUM(completion_tokens),0) FROM api_usage"
           " WHERE role = ? AND created >= ? AND created < ?"
           " GROUP BY endpoint"),
        (role, start, end))
    for ep, c, pt, ct in cur.fetchall():
        by_endpoint[ep or "?"] = {"calls": c, "tokens": (pt or 0) + (ct or 0)}
    conn.close()
    return {"calls": row[0], "prompt_tokens": row[1], "completion_tokens": row[2],
            "by_endpoint": by_endpoint}


def get_recent_api_usage(role=None, limit=50):
    """Recent API calls, newest first. Filter by role when given."""
    conn = _connect()
    cur = conn.cursor()
    if role:
        cur.execute(
            _q("SELECT endpoint, model, prompt_tokens, completion_tokens,"
               " created, role FROM api_usage WHERE role = ?"
               " ORDER BY id DESC LIMIT ?"),
            (role, limit))
    else:
        cur.execute(
            _q("SELECT endpoint, model, prompt_tokens, completion_tokens,"
               " created, role FROM api_usage ORDER BY id DESC LIMIT ?"),
            (limit,))
    rows = _rows_to_dicts(cur.fetchall(), cur)
    conn.close()
    return rows


def reset_rate_limits(role, day=None):
    """Clear today's rate-limit counters for `role`. Admin management tool."""
    day = day or _rate_limit_day()
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        _q("DELETE FROM rate_limit_counters WHERE role = ? AND day = ?"),
        (role, day))
    conn.commit()
    conn.close()


def set_image_owner(filename, owner):
    """Record which data partition uploaded an image file.

    Insert-only: if a row for `filename` already exists, its owner is NEVER
    changed. Combined with unguessable uuid filenames in app._save_upload,
    this guarantees a colliding upload can neither read nor reassign another
    partition's image.
    """
    if owner not in ("private", "public"):
        raise ValueError(f"Invalid owner: {owner!r}")
    conn = _connect()
    cur = conn.cursor()
    if USE_POSTGRES:
        cur.execute(_q(
            "INSERT INTO image_owners (filename, owner, created) VALUES (?, ?, ?)"
            " ON CONFLICT (filename) DO NOTHING"),
            (filename, owner, time.time()))
    else:
        cur.execute(
            "INSERT OR IGNORE INTO image_owners (filename, owner, created)"
            " VALUES (?, ?, ?)",
            (filename, owner, time.time()))
    conn.commit()
    conn.close()


def get_image_owner(filename):
    """Return the owning partition for an image file, or None if unknown.

    Unknown (pre-migration) images are treated as 'private' by callers.
    """
    conn = _connect()
    cur = conn.cursor()
    cur.execute(_q("SELECT owner FROM image_owners WHERE filename = ?"),
                (filename,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


# ---------- Rate limiting for restricted roles ----------
# Daily per-(role, op) counters. Used to cap the public test account's
# consumption of costly operations (rubric generation, grading).

def _rate_limit_day():
    """Current day as YYYY-MM-DD in server local time."""
    return time.strftime("%Y-%m-%d", time.localtime())


def check_rate_limit(role, op, limit):
    """Return True and record one hit if (role, op) is under `limit` today.

    Returns False without recording when the limit is reached. Not thread-safe
    against races, but over-admission by 1 under concurrency is acceptable for
    a demo guardrail.
    """
    day = _rate_limit_day()
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        _q("SELECT count FROM rate_limit_counters"
           " WHERE role = ? AND op = ? AND day = ?"),
        (role, op, day))
    row = cur.fetchone()
    current = row[0] if row else 0
    if current >= limit:
        conn.close()
        return False
    if row:
        cur.execute(
            _q("UPDATE rate_limit_counters SET count = count + 1"
               " WHERE role = ? AND op = ? AND day = ?"),
            (role, op, day))
    else:
        cur.execute(
            _q("INSERT INTO rate_limit_counters (role, op, day, count)"
               " VALUES (?, ?, ?, 1)"),
            (role, op, day))
    conn.commit()
    conn.close()
    return True


def get_rate_limit_usage(role, op, day=None):
    """Return today's hit count for (role, op). Used by tests and status UI."""
    day = day or _rate_limit_day()
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        _q("SELECT count FROM rate_limit_counters"
           " WHERE role = ? AND op = ? AND day = ?"),
        (role, op, day))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else 0


def add_lifetime_usage(role, op, amount):
    """Add `amount` to the lifetime counter for (role, op).

    Lifetime counters use day='lifetime' in the same table. Used to cap the
    total number of questions the public demo account may ever create/fix.
    """
    if amount <= 0:
        return
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        _q("SELECT count FROM rate_limit_counters"
           " WHERE role = ? AND op = ? AND day = 'lifetime'"),
        (role, op))
    row = cur.fetchone()
    if row:
        cur.execute(
            _q("UPDATE rate_limit_counters SET count = count + ?"
               " WHERE role = ? AND op = ? AND day = 'lifetime'"),
            (amount, role, op))
    else:
        cur.execute(
            _q("INSERT INTO rate_limit_counters (role, op, day, count)"
               " VALUES (?, ?, 'lifetime', ?)"),
            (role, op, amount))
    conn.commit()
    conn.close()


def get_lifetime_usage(role, op):
    """Return the lifetime total for (role, op)."""
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        _q("SELECT count FROM rate_limit_counters"
           " WHERE role = ? AND op = ? AND day = 'lifetime'"),
        (role, op))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else 0
