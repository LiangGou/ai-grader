"""SQLite data layer: table setup + CRUD (stdlib sqlite3 only, no ORM)."""
import json
import os
import sqlite3
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
def save_grade(assignment_id, student_name, questions, total, max_total, overall_feedback):
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO grades
           (assignment_id, student_name, questions_json, total, max_total, overall_feedback, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            assignment_id,
            student_name,
            json.dumps(questions, ensure_ascii=False),
            total,
            max_total,
            overall_feedback,
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
