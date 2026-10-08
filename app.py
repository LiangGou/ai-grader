"""AI Grader web app for teachers (Flask backend)."""
import os

# Sanitize the proxy bypass list BEFORE anything imports httpx (via the OpenAI
# SDK). Some sandboxes inject bracketed IPv6 entries like "[::1]" into
# NO_PROXY/no_proxy, which httpx cannot parse and fails with
# "Invalid port: ':1]'" on every API call. Keep a minimal, parseable list;
# api.openai.com still goes through the egress proxy as intended.
for _pv in ("NO_PROXY", "no_proxy"):
    _vv = os.environ.get(_pv, "")
    if "[::1]" in _vv or "*" in _vv:
        os.environ[_pv] = "localhost,127.0.0.1,::1"

import csv
import io
import json
import random
import threading
import time
import uuid
from datetime import datetime

from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request, send_file, url_for

import db
import grader

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024  # max 32MB per request

db.init_db()


def _save_upload(file_storage, prefix):
    ext = os.path.splitext(file_storage.filename or "")[1].lower()
    if ext not in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
        ext = ".jpg"
    name = f"{prefix}_{int(time.time())}_{random.randint(1000, 9999)}{ext}"
    path = os.path.join(UPLOAD_DIR, name)
    file_storage.save(path)
    return path


def _env_status():
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    return {
        "key_set": bool(key),
        "key_hint": ("…" + key[-4:]) if key and len(key) > 4 else "",
        "model": os.environ.get("OPENAI_MODEL", grader.DEFAULT_MODEL),
    }


# ---------- Home: classes / assignments ----------
@app.route("/")
def index():
    classes = db.list_classes()
    for c in classes:
        c["assignments"] = db.list_assignments(c["id"])
    dup = request.args.get("dup")
    return render_template("index.html", classes=classes, env=_env_status(),
                           dup_name=dup)


@app.route("/class/new", methods=["POST"])
def class_new():
    name = (request.form.get("name") or "").strip()
    if name:
        db.create_class(name)
    return redirect(url_for("index"))


@app.route("/assignment/new", methods=["POST"])
def assignment_new():
    class_id = request.form.get("class_id")
    name = (request.form.get("name") or "").strip()
    if class_id and name:
        try:
            aid = db.create_assignment(int(class_id), name)
        except ValueError:
            # Duplicate name in this class — bounce back with an error flag.
            return redirect(url_for("index", dup=name))
        return redirect(url_for("assignment_page", assignment_id=aid))
    return redirect(url_for("index"))


# ---------- Assignment setup: rubric entry ----------
@app.route("/assignment/<int:assignment_id>")
def assignment_page(assignment_id):
    a = db.get_assignment(assignment_id)
    if not a:
        return redirect(url_for("index"))
    items = json.loads(a["rubric_json"] or "[]")
    # Self-heal: normalize LaTeX delimiters in rubrics saved before the fix.
    for it in items:
        for k in ("question_text", "reference_solution", "criteria"):
            if it.get(k):
                it[k] = grader.normalize_latex(it[k])
    refs = json.loads(a["reference_images"] or "[]")
    refs = [os.path.basename(p) for p in refs if os.path.exists(p)]
    rubric_examples = db.get_rubric_example_images(assignment_id)
    rubric_examples = [os.path.basename(p) for p in rubric_examples if os.path.exists(p)]
    return render_template(
        "assignment.html", a=a, items=items, refs=refs,
        rubric_examples=rubric_examples, env=_env_status()
    )


# Background rubric-digitization jobs: digitizing + validating takes ~1 minute,
# too long for one fragile HTTP request. POST /api/rubric_from_example starts
# a job and returns immediately; the client polls /api/rubric_job/<job_id>.
@app.route("/api/rubric_job/<job_id>")
def api_rubric_job(job_id):
    # Jobs are DB-backed (not in-memory) so they survive gunicorn multi-worker
    # setups and server restarts. A "running" job with no update for a long
    # time means its worker died: report it as an error, not "not found".
    job = db.get_rubric_job(job_id)
    if not job:
        return jsonify({"ok": False, "error": "Job not found. The server may have restarted — please tap Generate again."}), 404
    if job["status"] == "running" and time.time() - job["updated"] > db.JOB_STALE_SECONDS:
        db.fail_rubric_job(job_id, "The generation worker stopped unexpectedly — please tap Generate again.")
        job = db.get_rubric_job(job_id)
    # While running, "done" carries the questions validated so far (each with
    # "idx"); the client renders and saves them incrementally.
    out = {"ok": True, "status": job["status"], "total": job.get("total", 0),
           "done": job.get("done", [])}
    if job["status"] == "done":
        out["items"] = job["items"]
    elif job["status"] == "error":
        out["error"] = job.get("error", "Unknown error.")
    return jsonify(out)


@app.route("/api/solve_rubric", methods=["POST"])
def api_solve_rubric():
    """Start the unified rubric job; returns immediately.

    One button does both: the AI digitizes the photos (printed questions +
    point values; teacher's worked solutions/answer key IF shown). Per
    question: if a teacher reference solution was found it is validated and
    used (AP-style rubric auto-generated when the markings yield no usable
    rules); otherwise the AI solves it itself with the reasoning model
    (up to 3 attempts) and builds an AP-style rubric. Max scores always come
    from the printed point values. The client polls /api/rubric_job/<job_id>.
    """
    data = request.get_json(force=True, silent=True) or {}
    assignment_id = data.get("assignment_id")
    include = data.get("include") or []
    if not assignment_id:
        return jsonify({"ok": False, "error": "Missing assignment."}), 400
    saved = db.get_rubric_example_images(assignment_id)
    saved_names = {os.path.basename(p): p for p in saved if os.path.exists(p)}
    paths = [saved_names[n] for n in include if n in saved_names]
    if not paths:
        return jsonify({"ok": False, "error": "No question photos selected. Please add them first."}), 400
    job_id = uuid.uuid4().hex[:12]
    db.cleanup_old_rubric_jobs()
    db.create_rubric_job(job_id)
    threading.Thread(target=_run_solve_job, args=(job_id, paths), daemon=True).start()
    return jsonify({"ok": True, "job_id": job_id})


def _run_solve_job(job_id, paths):
    # Job state lives in the DB (not process memory) so any gunicorn worker
    # can serve poll requests and jobs survive restarts.
    def _on_total(total):
        db.set_rubric_job_total(job_id, total)

    def _on_item(item):
        db.append_rubric_job_item(job_id, item)

    try:
        result = grader.build_unified_rubric_streaming(
            paths, upload_dir=UPLOAD_DIR, on_total=_on_total, on_item=_on_item)
        db.finish_rubric_job(job_id, result["items"])
    except RuntimeError as e:
        db.fail_rubric_job(job_id, str(e))
    except Exception as e:  # noqa: BLE001
        db.fail_rubric_job(job_id, f"Failed to solve and generate rubric: {e}")




@app.route("/api/retry_question", methods=["POST"])
def api_retry_question():
    """Retry AI-solving a single question with debug output.

    Body: {assignment_id, q, question_text, max_score, model, high_effort}.
    `model` is "o3" (default) or "gpt-4o"; `high_effort` requests o3 with
    reasoning_effort="high". Returns the new solution, criteria, verification
    status, and the raw model outputs for debugging.
    """
    data = request.get_json(force=True, silent=True) or {}
    assignment_id = data.get("assignment_id")
    q_label = str(data.get("q", "")).strip()
    question_text = str(data.get("question_text", "")).strip()
    model = str(data.get("model", "o3")).strip() or "o3"
    high_effort = bool(data.get("high_effort"))
    try:
        max_score = float(data.get("max_score", 0) or 0)
    except (TypeError, ValueError):
        max_score = 0
    if not assignment_id or not question_text:
        return jsonify({"ok": False, "error": "Missing assignment or question text."}), 400
    if model not in ("o3", "gpt-4o"):
        return jsonify({"ok": False, "error": "Model must be 'o3' or 'gpt-4o'."}), 400
    # Give the solver the assignment's question photos for a second read.
    image_paths = [p for p in db.get_rubric_example_images(assignment_id) if os.path.exists(p)]
    try:
        result = grader.retry_solve_question(
            question_text, max_score, q_label,
            model=model, high_effort=high_effort,
            image_paths=image_paths or None,
        )
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"Retry failed: {e}"}), 500
    return jsonify({"ok": True, "result": result})

@app.route("/assignment/<int:assignment_id>/upload_rubric_example", methods=["POST"])
def assignment_upload_rubric_example(assignment_id):
    a = db.get_assignment(assignment_id)
    if not a:
        return jsonify({"ok": False, "error": "Assignment not found."}), 404
    existing = db.get_rubric_example_images(assignment_id)
    for f in request.files.getlist("images"):
        if f and f.filename:
            existing.append(_save_upload(f, "rubric_example"))
    db.save_rubric_example_images(assignment_id, existing)
    names = [os.path.basename(p) for p in existing if os.path.exists(p)]
    return jsonify({"ok": True, "examples": names})


@app.route("/assignment/<int:assignment_id>/delete_rubric_example", methods=["POST"])
def assignment_delete_rubric_example(assignment_id):
    data = request.get_json(force=True, silent=True) or {}
    name = os.path.basename(data.get("filename") or "")
    existing = db.get_rubric_example_images(assignment_id)
    kept = []
    for p in existing:
        if os.path.basename(p) == name:
            try:
                os.remove(p)
            except OSError:
                pass
        else:
            kept.append(p)
    db.save_rubric_example_images(assignment_id, kept)
    names = [os.path.basename(p) for p in kept if os.path.exists(p)]
    return jsonify({"ok": True, "examples": names})


@app.route("/assignment/<int:assignment_id>/save_rubric", methods=["POST"])
def assignment_save_rubric(assignment_id):
    data = request.get_json(force=True, silent=True) or {}
    items = data.get("items") or []
    # The form's items win (the teacher may have edited them after AI generation).
    clean = []
    for it in items:
        q = str(it.get("q", "")).strip()
        if not q:
            continue
        try:
            ms = float(it.get("max_score", 0))
        except (TypeError, ValueError):
            ms = 0
        clean.append({
            "q": q,
            "max_score": ms,
            "criteria": str(it.get("criteria", "")).strip(),
            "question_text": str(it.get("question_text", "")).strip(),
            "reference_solution": str(it.get("reference_solution", "")).strip(),
            "verified": it.get("verified", "review") if it.get("verified") in ("yes", "review") else "review",
            "verify_note": str(it.get("verify_note", "")).strip(),
            "graph": it.get("graph") if isinstance(it.get("graph"), dict) else None,
        })
    items = clean
    rubric_text = grader.rubric_items_to_text(items)
    if not rubric_text.strip():
        return jsonify({"ok": False, "error": "The rubric is empty. Please generate it from a reference example in Step 1 first."}), 400
    db.save_rubric(assignment_id, rubric_text, items)
    return jsonify({"ok": True, "rubric_text": rubric_text, "items": items})


@app.route("/assignment/<int:assignment_id>/upload_reference", methods=["POST"])
def assignment_upload_reference(assignment_id):
    files = request.files.getlist("images")
    a = db.get_assignment(assignment_id)
    if not a:
        return redirect(url_for("index"))
    existing = json.loads(a["reference_images"] or "[]")
    for f in files:
        if f and f.filename:
            existing.append(_save_upload(f, "ref"))
    db.save_reference_images(assignment_id, existing)
    return redirect(url_for("assignment_page", assignment_id=assignment_id))


# ---------- Grading ----------
@app.route("/api/assignment/<int:assignment_id>/delete", methods=["POST"])
def api_assignment_delete(assignment_id):
    if not db.get_assignment(assignment_id):
        return jsonify({"ok": False, "error": "Assignment not found."}), 404
    db.delete_assignment(assignment_id)
    return jsonify({"ok": True})


@app.route("/grade/<int:assignment_id>")
def grade_page(assignment_id):
    a = db.get_assignment(assignment_id)
    if not a:
        return redirect(url_for("index"))
    return render_template("grade.html", a=a, env=_env_status())


@app.route("/api/grade", methods=["POST"])
def api_grade():
    assignment_id = request.form.get("assignment_id")
    student_name = (request.form.get("student_name") or "").strip()
    if not assignment_id:
        return jsonify({"ok": False, "error": "Missing assignment ID."}), 400
    if not student_name:
        return jsonify({"ok": False, "error": "Please enter the student name first."}), 400
    a = db.get_assignment(int(assignment_id))
    if not a:
        return jsonify({"ok": False, "error": "Assignment not found."}), 404

    files = request.files.getlist("images")
    paths = []
    for f in files:
        if f and f.filename:
            paths.append(_save_upload(f, "paper"))
    if not paths:
        return jsonify({"ok": False, "error": "No paper photos received. Please take/upload photos first."}), 400

    items = json.loads(a["rubric_json"] or "[]")
    ref_paths = [p for p in json.loads(a["reference_images"] or "[]") if os.path.exists(p)]
    try:
        result = grader.grade_paper(a["rubric_text"], items, paths, ref_paths)
    except RuntimeError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"Grading failed: {e}"}), 500
    return jsonify({"ok": True, "result": result, "photos": [os.path.basename(p) for p in paths]})


@app.route("/api/save_grade", methods=["POST"])
def api_save_grade():
    data = request.get_json(force=True, silent=True) or {}
    try:
        gid = db.save_grade(
            int(data["assignment_id"]),
            (data.get("student_name") or "").strip(),
            data.get("questions") or [],
            float(data.get("total", 0)),
            float(data.get("max_total", 0)),
            data.get("overall_feedback") or "",
            data.get("photos") or [],
        )
    except (KeyError, TypeError, ValueError) as e:
        return jsonify({"ok": False, "error": f"Save failed, incomplete data: {e}"}), 400
    return jsonify({"ok": True, "id": gid})


# ---------- Results & stats ----------
@app.route("/results/<int:assignment_id>")
def results_page(assignment_id):
    a = db.get_assignment(assignment_id)
    if not a:
        return redirect(url_for("index"))
    grades = db.list_grades(assignment_id)
    totals = [g["total"] for g in grades]
    stats = {}
    if totals:
        stats = {
            "count": len(totals),
            "avg": round(sum(totals) / len(totals), 2),
            "max": max(totals),
            "min": min(totals),
        }
    return render_template("results.html", a=a, grades=grades, stats=stats, env=_env_status())


@app.route("/results/<int:assignment_id>/csv")
def results_csv(assignment_id):
    a = db.get_assignment(assignment_id)
    if not a:
        return redirect(url_for("index"))
    grades = db.list_grades(assignment_id)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Name", "Total", "Max", "Overall feedback", "Graded at"])
    for g in grades:
        w.writerow([g["student_name"], g["total"], g["max_total"], g["overall_feedback"], g["created_at"]])
    mem = io.BytesIO(buf.getvalue().encode("utf-8-sig"))
    mem.seek(0)
    filename = f"grades_{a['name']}_{datetime.now().strftime('%Y%m%d')}.csv"
    return send_file(mem, mimetype="text/csv", as_attachment=True, download_name=filename)


@app.route("/graded/<int:grade_id>")
def grade_detail_page(grade_id):
    g = db.get_grade(grade_id)
    if not g:
        return redirect(url_for("index"))
    a = db.get_assignment(g["assignment_id"])
    try:
        questions = json.loads(g["questions_json"] or "[]")
    except (json.JSONDecodeError, TypeError):
        questions = []
    try:
        photos = json.loads(g.get("images") or "[]")
    except (json.JSONDecodeError, TypeError):
        photos = []
    # Only keep photos that still exist on disk.
    photos = [p for p in photos if os.path.exists(os.path.join(UPLOAD_DIR, p))]
    return render_template("grade_detail.html", a=a, g=g, questions=questions, photos=photos, env=_env_status())


# ---------- Settings ----------
@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    msg = ""
    if request.method == "POST":
        api_key = (request.form.get("api_key") or "").strip()
        model = (request.form.get("model") or "").strip() or grader.DEFAULT_MODEL
        _write_env(api_key, model)
        # Apply to the current process as well.
        if api_key:
            os.environ["OPENAI_API_KEY"] = api_key
        os.environ["OPENAI_MODEL"] = model
        msg = "Saved."
    return render_template("settings.html", env=_env_status(), msg=msg)


def _write_env(api_key, model):
    """Write the key into the project-root .env (server-local only, never committed to git)."""
    env_path = os.path.join(BASE_DIR, ".env")
    lines = []
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    out, seen_key, seen_model = [], False, False
    for ln in lines:
        if ln.startswith("OPENAI_API_KEY="):
            out.append(f"OPENAI_API_KEY={api_key}" if api_key else ln)
            seen_key = True
        elif ln.startswith("OPENAI_MODEL="):
            out.append(f"OPENAI_MODEL={model}")
            seen_model = True
        else:
            out.append(ln)
    if api_key and not seen_key:
        out.append(f"OPENAI_API_KEY={api_key}")
    if not seen_model:
        out.append(f"OPENAI_MODEL={model}")
    with open(env_path, "w", encoding="utf-8") as f:
        f.write("\n".join(out).strip() + "\n")


@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    # Block path traversal (e.g. ../../ai_grader.db): only serve files
    # physically inside UPLOAD_DIR.
    safe = os.path.realpath(os.path.join(UPLOAD_DIR, filename))
    if not safe.startswith(os.path.realpath(UPLOAD_DIR) + os.sep):
        return jsonify({"ok": False, "error": "Not found."}), 404
    if not os.path.isfile(safe):
        return jsonify({"ok": False, "error": "Not found."}), 404
    return send_file(safe)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
