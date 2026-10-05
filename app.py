"""AI 判卷 / 判作业 web app（Flask 后端）。"""
import csv
import io
import json
import os
import random
import time
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
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024  # 单次请求最多 32MB

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


# ---------- 首页：班级 / 任务 ----------
@app.route("/")
def index():
    classes = db.list_classes()
    for c in classes:
        c["assignments"] = db.list_assignments(c["id"])
    return render_template("index.html", classes=classes, env=_env_status())


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
        aid = db.create_assignment(int(class_id), name)
        return redirect(url_for("assignment_page", assignment_id=aid))
    return redirect(url_for("index"))


# ---------- 任务设置：rubric 录入 ----------
@app.route("/assignment/<int:assignment_id>")
def assignment_page(assignment_id):
    a = db.get_assignment(assignment_id)
    if not a:
        return redirect(url_for("index"))
    items = json.loads(a["rubric_json"] or "[]")
    refs = json.loads(a["reference_images"] or "[]")
    refs = [os.path.basename(p) for p in refs if os.path.exists(p)]
    return render_template(
        "assignment.html", a=a, items=items, refs=refs, env=_env_status()
    )


@app.route("/api/structure_rubric", methods=["POST"])
def api_structure_rubric():
    data = request.get_json(force=True, silent=True) or {}
    transcript = (data.get("transcript") or "").strip()
    if not transcript:
        return jsonify({"ok": False, "error": "语音记录是空的，请先录入或输入判分规则。"}), 400
    try:
        result = grader.structure_rubric(transcript)
    except RuntimeError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"整理 rubric 失败：{e}"}), 500
    return jsonify({"ok": True, "items": result["items"]})


@app.route("/assignment/<int:assignment_id>/save_rubric", methods=["POST"])
def assignment_save_rubric(assignment_id):
    data = request.get_json(force=True, silent=True) or {}
    mode = data.get("mode", "structured")  # structured | text
    items = data.get("items") or []
    if mode == "text":
        rubric_text = (data.get("raw_text") or "").strip()
        items = []
    else:
        # 结构化模式：以表单里的 items 为准（老师可能已手动修改）
        clean = []
        for it in items:
            q = str(it.get("q", "")).strip()
            if not q:
                continue
            try:
                ms = float(it.get("max_score", 0))
            except (TypeError, ValueError):
                ms = 0
            clean.append({"q": q, "max_score": ms, "criteria": str(it.get("criteria", "")).strip()})
        items = clean
        rubric_text = grader.rubric_items_to_text(items)
    if not rubric_text.strip():
        return jsonify({"ok": False, "error": "rubric 是空的，请先录入评分规则。"}), 400
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


# ---------- 判卷 ----------
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
        return jsonify({"ok": False, "error": "缺少任务 ID。"}), 400
    if not student_name:
        return jsonify({"ok": False, "error": "请先填写学生姓名。"}), 400
    a = db.get_assignment(int(assignment_id))
    if not a:
        return jsonify({"ok": False, "error": "任务不存在。"}), 404

    files = request.files.getlist("images")
    paths = []
    for f in files:
        if f and f.filename:
            paths.append(_save_upload(f, "paper"))
    if not paths:
        return jsonify({"ok": False, "error": "没有收到答卷照片，请先拍照上传。"}), 400

    items = json.loads(a["rubric_json"] or "[]")
    ref_paths = [p for p in json.loads(a["reference_images"] or "[]") if os.path.exists(p)]
    try:
        result = grader.grade_paper(a["rubric_text"], items, paths, ref_paths)
    except RuntimeError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"判分失败：{e}"}), 500
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
        )
    except (KeyError, TypeError, ValueError) as e:
        return jsonify({"ok": False, "error": f"保存失败，数据不完整：{e}"}), 400
    return jsonify({"ok": True, "id": gid})


# ---------- 成绩统计 ----------
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
    w.writerow(["姓名", "总分", "满分", "整体评语", "判卷时间"])
    for g in grades:
        w.writerow([g["student_name"], g["total"], g["max_total"], g["overall_feedback"], g["created_at"]])
    mem = io.BytesIO(buf.getvalue().encode("utf-8-sig"))
    mem.seek(0)
    filename = f"成绩_{a['name']}_{datetime.now().strftime('%Y%m%d')}.csv"
    return send_file(mem, mimetype="text/csv", as_attachment=True, download_name=filename)


# ---------- 设置 ----------
@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    msg = ""
    if request.method == "POST":
        api_key = (request.form.get("api_key") or "").strip()
        model = (request.form.get("model") or "").strip() or grader.DEFAULT_MODEL
        _write_env(api_key, model)
        # 让当前进程也生效
        if api_key:
            os.environ["OPENAI_API_KEY"] = api_key
        os.environ["OPENAI_MODEL"] = model
        msg = "已保存。"
    return render_template("settings.html", env=_env_status(), msg=msg)


def _write_env(api_key, model):
    """把 key 写进项目根目录的 .env（只存服务器本地，不进 git）。"""
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
    return send_file(os.path.join(UPLOAD_DIR, filename))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
