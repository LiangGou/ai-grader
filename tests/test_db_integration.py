#!/usr/bin/env python3
"""Integration tests for the db layer (SQLite + Postgres dual mode).

- SQLite mode: always runs (uses a temp DB file).
- Postgres mode: runs only if TEST_DATABASE_URL is set (e.g. Neon).
  Never touches the production DATABASE_URL or the dev ai_grader.db.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, "/home/hatch/workspace/ai-grader")

errors = []


def check(cond, msg):
    if not cond:
        errors.append(msg)
        print(f"  ❌ {msg}")
    else:
        print(f"  ✅ {msg}")


def run_crud_suite(db_mod, label):
    print(f"\n[{label}] CRUD suite")
    db_mod.init_db()

    # Classes
    cid = db_mod.create_class("Test Class")
    check(isinstance(cid, int) and cid > 0, "create_class returns id")
    classes = db_mod.list_classes()
    check(any(c["id"] == cid for c in classes), "list_classes contains new class")
    check(db_mod.get_class(cid)["name"] == "Test Class", "get_class")

    # Assignments
    aid = db_mod.create_assignment(cid, "Quiz 1")
    check(isinstance(aid, int) and aid > 0, "create_assignment returns id")
    try:
        db_mod.create_assignment(cid, "Quiz 1")
        check(False, "duplicate assignment name rejected")
    except ValueError:
        check(True, "duplicate assignment name rejected")
    check(db_mod.get_assignment(aid)["name"] == "Quiz 1", "get_assignment")

    # Rubric
    db_mod.save_rubric(aid, "rubric text", [{"q": "1", "criteria": "x"}])
    a = db_mod.get_assignment(aid)
    check(a["rubric_text"] == "rubric text", "save_rubric text")
    check(json.loads(a["rubric_json"])[0]["q"] == "1", "save_rubric json")

    # Grades
    gid = db_mod.save_grade(aid, "Alice", [{"q": "1", "score": 5}], 5, 10, "good", images=["p1.jpg"])
    check(isinstance(gid, int) and gid > 0, "save_grade returns id")
    grades = db_mod.list_grades(aid)
    check(len(grades) == 1 and grades[0]["student_name"] == "Alice", "list_grades")
    check(db_mod.get_grade(gid)["total"] == 5, "get_grade")

    # Rubric jobs
    db_mod.create_rubric_job("job-1")
    job = db_mod.get_rubric_job("job-1")
    check(job and job["status"] == "running", "create/get rubric job")
    db_mod.set_rubric_job_total("job-1", 3)
    db_mod.append_rubric_job_item("job-1", {"q": "1"})
    db_mod.finish_rubric_job("job-1", [{"q": "1"}])
    job = db_mod.get_rubric_job("job-1")
    check(job["status"] == "done" and len(job["items"]) == 1, "finish rubric job")

    # API usage
    db_mod.log_api_usage("test", "gpt-4o", 100, 50)
    summary = db_mod.get_api_usage_summary()
    check(summary["calls"] >= 1, "api usage logged")

    # Cleanup
    db_mod.delete_assignment(aid)
    check(db_mod.get_assignment(aid) is None, "delete_assignment")
    print(f"[{label}] done")


def main():
    # --- SQLite mode (temp file) ---
    tmp = tempfile.mktemp(suffix=".db")
    os.environ.pop("DATABASE_URL", None)
    # Force reimport with fresh env
    for mod in list(sys.modules):
        if mod == "db" or mod.startswith("db."):
            del sys.modules[mod]
    import db as sqlite_db
    sqlite_db.DB_PATH = tmp
    run_crud_suite(sqlite_db, "SQLite")
    try:
        os.unlink(tmp)
    except OSError:
        pass

    # --- Postgres mode (only if TEST_DATABASE_URL set) ---
    test_url = os.environ.get("TEST_DATABASE_URL", "").strip()
    if test_url:
        os.environ["DATABASE_URL"] = test_url
        for mod in list(sys.modules):
            if mod == "db" or mod.startswith("db."):
                del sys.modules[mod]
        import db as pg_db
        if not pg_db.USE_POSTGRES:
            check(False, "Postgres driver not available")
        else:
            run_crud_suite(pg_db, "Postgres")
    else:
        print("\n[Postgres] SKIPPED (set TEST_DATABASE_URL to run)")

    print()
    if errors:
        print(f"❌ {len(errors)} failures")
        sys.exit(1)
    print("✅ All DB integration tests passed!")


if __name__ == "__main__":
    main()
