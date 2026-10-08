"""SQLite data layer: table setup + CRUD (stdlib sqlite3 only, no ORM)."""
import json
import os
import sqlite3
import time
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ai_grader.db")


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = _connect()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS classes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS assignments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            class_id INTEGER NOT NULL REFERENCES classes(id),
            name TEXT NOT NULL,
            rubric_text TEXT DEFAULT '',
            rubric_json TEXT DEFAULT '[]',
            reference_images TEXT DEFAULT '[]',
            created_at TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS grades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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
    cols = [r[1] for r in cur.execute("PRAGMA table_info(assignments)").fetchall()]
    if "rubric_example_images" not in cols:
        cur.execute("ALTER TABLE assignments ADD COLUMN rubric_example_images TEXT DEFAULT '[]'")
    # Migration: saved AP standard comparison (added after initial release).
    if "ap_comparison" not in cols:
        cur.execute("ALTER TABLE assignments ADD COLUMN ap_comparison TEXT DEFAULT ''")
    # Migration: student paper photos linked to each grade.
    gcols = [r[1] for r in cur.execute("PRAGMA table_info(grades)").fetchall()]
    if "images" not in gcols:
        cur.execute("ALTER TABLE grades ADD COLUMN images TEXT DEFAULT '[]'")
    # Rubric generation jobs: DB-backed so jobs survive multi-worker gunicorn
    # and Render restarts (in-memory dict lost jobs with --workers 2).
    cur.execute("""
        CREATE TABLE IF NOT EXISTS rubric_jobs (
            job_id TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'running',
            total INTEGER NOT NULL DEFAULT 0,
            done_json TEXT NOT NULL DEFAULT '[]',
            items_json TEXT NOT NULL DEFAULT '[]',
            error TEXT DEFAULT '',
            created REAL NOT NULL,
            updated REAL NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------- Classes ----------
def create_class(name):
    conn = _connect()
    cur = conn.cursor()
    cur.execute("INSERT INTO classes (name, created_at) VALUES (?, ?)", (name, now()))
    cid = cur.lastrowid
    conn.commit()
    conn.close()
    return cid


def list_classes():
    conn = _connect()
    rows = conn.execute("SELECT * FROM classes ORDER BY id DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_class(class_id):
    conn = _connect()
    row = conn.execute("SELECT * FROM classes WHERE id = ?", (class_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


# ---------- Grading assignments ----------
def create_assignment(class_id, name):
    conn = _connect()
    cur = conn.cursor()
    # Reject duplicate names within the same class (user directive 2026-10-07).
    cur.execute(
        "SELECT id FROM assignments WHERE class_id = ? AND name = ?",
        (class_id, name),
    )
    if cur.fetchone():
        conn.close()
        raise ValueError(f"Assignment '{name}' already exists in this class.")
    cur.execute(
        "INSERT INTO assignments (class_id, name, created_at) VALUES (?, ?, ?)",
        (class_id, name, now()),
    )
    aid = cur.lastrowid
    conn.commit()
    conn.close()
    return aid


def list_assignments(class_id):
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM assignments WHERE class_id = ? ORDER BY id DESC", (class_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_assignment(assignment_id):
    conn = _connect()
    row = conn.execute("SELECT * FROM assignments WHERE id = ?", (assignment_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def save_rubric(assignment_id, rubric_text, rubric_items):
    """Save the structured rubric (JSON) plus the auto-generated plain-text version."""
    conn = _connect()
    conn.execute(
        "UPDATE assignments SET rubric_text = ?, rubric_json = ? WHERE id = ?",
        (rubric_text, json.dumps(rubric_items, ensure_ascii=False), assignment_id),
    )
    conn.commit()
    conn.close()


def save_reference_images(assignment_id, paths):
    conn = _connect()
    conn.execute(
        "UPDATE assignments SET reference_images = ? WHERE id = ?",
        (json.dumps(paths, ensure_ascii=False), assignment_id),
    )
    conn.commit()
    conn.close()


# ---------- Grades ----------
def save_grade(assignment_id, student_name, questions, total, max_total, overall_feedback, images=None):
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
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
        ),
    )
    gid = cur.lastrowid
    conn.commit()
    conn.close()
    return gid


def list_grades(assignment_id):
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM grades WHERE assignment_id = ? ORDER BY total DESC, id ASC",
        (assignment_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_grade(grade_id):
    conn = _connect()
    row = conn.execute("SELECT * FROM grades WHERE id = ?", (grade_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


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
    conn.execute(
        "UPDATE assignments SET rubric_example_images = ? WHERE id = ?",
        (json.dumps(paths, ensure_ascii=False), assignment_id),
    )
    conn.commit()
    conn.close()


def save_ap_comparison(assignment_id, comparison):
    """Persist the AP standard comparison so suggestions survive page refresh."""
    conn = _connect()
    conn.execute(
        "UPDATE assignments SET ap_comparison = ? WHERE id = ?",
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
    conn.execute("DELETE FROM grades WHERE assignment_id = ?", (assignment_id,))
    conn.execute("DELETE FROM assignments WHERE id = ?", (assignment_id,))
    conn.commit()
    conn.close()


# ---------- Rubric generation jobs (DB-backed) ----------
# Jobs were kept in an in-memory dict, which broke under gunicorn --workers 2
# (each worker has its own memory: job created in worker A, poll hits worker B
# -> "Job not found") and on Render restarts. DB storage fixes both.

JOB_STALE_SECONDS = 900  # a "running" job with no update for 15 min is dead


def create_rubric_job(job_id):
    conn = _connect()
    now_ts = time.time()
    conn.execute(
        "INSERT OR REPLACE INTO rubric_jobs (job_id, status, total, done_json, items_json, error, created, updated)"
        " VALUES (?, 'running', 0, '[]', '[]', '', ?, ?)",
        (job_id, now_ts, now_ts),
    )
    conn.commit()
    conn.close()


def get_rubric_job(job_id):
    conn = _connect()
    row = conn.execute("SELECT * FROM rubric_jobs WHERE job_id = ?", (job_id,)).fetchone()
    conn.close()
    if not row:
        return None
    return {
        "job_id": row["job_id"],
        "status": row["status"],
        "total": row["total"],
        "done": json.loads(row["done_json"] or "[]"),
        "items": json.loads(row["items_json"] or "[]"),
        "error": row["error"] or "",
        "created": row["created"],
        "updated": row["updated"],
    }


def _touch_job(conn, job_id):
    conn.execute("UPDATE rubric_jobs SET updated = ? WHERE job_id = ?", (time.time(), job_id))


def set_rubric_job_total(job_id, total):
    conn = _connect()
    conn.execute("UPDATE rubric_jobs SET total = ? WHERE job_id = ?", (total, job_id))
    _touch_job(conn, job_id)
    conn.commit()
    conn.close()


def append_rubric_job_item(job_id, item):
    conn = _connect()
    row = conn.execute("SELECT done_json FROM rubric_jobs WHERE job_id = ?", (job_id,)).fetchone()
    if row:
        done = json.loads(row["done_json"] or "[]")
        done.append(item)
        conn.execute("UPDATE rubric_jobs SET done_json = ? WHERE job_id = ?", (json.dumps(done), job_id))
        _touch_job(conn, job_id)
        conn.commit()
    conn.close()


def finish_rubric_job(job_id, items):
    conn = _connect()
    payload = json.dumps(items)
    conn.execute(
        "UPDATE rubric_jobs SET status = 'done', items_json = ?, done_json = ?, total = ? WHERE job_id = ?",
        (payload, payload, len(items), job_id),
    )
    _touch_job(conn, job_id)
    conn.commit()
    conn.close()


def fail_rubric_job(job_id, error):
    conn = _connect()
    conn.execute("UPDATE rubric_jobs SET status = 'error', error = ? WHERE job_id = ?", (str(error), job_id))
    _touch_job(conn, job_id)
    conn.commit()
    conn.close()


def cleanup_old_rubric_jobs(max_age_seconds=1800):
    conn = _connect()
    conn.execute("DELETE FROM rubric_jobs WHERE created < ?", (time.time() - max_age_seconds,))
    conn.commit()
    conn.close()
