"""Call the OpenAI API for two jobs:
1. Read the teacher's reference example (a hand-graded paper, answer key, or
   scoring guide photo) with vision and generate a structured rubric from it;
2. Grade photos of a student's paper question-by-question against the rubric.

Images are sent as base64 data URLs; supports multiple paper photos plus
optional reference/answer-key images.
"""
import base64
import json
import os
import re

from dotenv import load_dotenv

load_dotenv()

DEFAULT_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o")

EXAMPLE_RUBRIC_SYSTEM = """You are digitizing a teacher's reference example (a hand-graded paper, answer key, or scoring guide) into structured data.
For each question, transcribe and extract:
- "q": the question label (e.g. "Question 1").
- "question_text": the question itself, transcribed faithfully. Write ALL mathematics in LaTeX: use $...$ for inline math (e.g. $2x + 6 = 14$) and $$...$$ for display math (e.g. $$\\int_0^1 x^2 dx$$). EVERY math fragment MUST be wrapped in $ delimiters — never leave LaTeX commands like \\frac outside $...$.
- "reference_solution": the teacher's correct solution / working / final answer, transcribed faithfully, with mathematics in LaTeX as above (every fragment wrapped in $...$). Capture the final answer at minimum; include key working steps if shown, separating distinct steps with a line break.
- "max_score": the max points for the question.
- "criteria": the scoring/deduction rules you can infer from the teacher's markings (check marks, crosses, circled mistakes, handwritten notes like "-2"). For example, if the teacher wrote "-2" next to a missing unit, include "deduct 2 points for a missing/wrong unit".
- "has_diagram": true or false — REQUIRED. Look carefully at the example photo: does this question show ANY diagram, graph, plot, coordinate system, or geometric figure? You must answer this for every question.
- "graph": REQUIRED when has_diagram is true, omitted otherwise. Describe the diagram ONE of these two ways:
  * If it is the plot of a single mathematical function y = f(x): {"kind": "function", "expression": "<Python expression in x using only numbers, x, +, -, *, /, **, parentheses, and these functions: sqrt, sin, cos, tan, log, log10, exp, abs, pi, e. Example: 'x**2 - 3*x + 2'>", "x_min": <number>, "x_max": <number>} — choose an x range that matches what the graph shows.
  * Otherwise (geometry figures, bar charts, hand-drawn diagrams, number lines, etc.): {"kind": "crop", "image_index": <0-based index of the example photo it appears in>, "x_min": <0-1000>, "y_min": <0-1000>, "x_max": <0-1000>, "y_max": <0-1000>} — the bounding box of the diagram within that photo, in relative 0-1000 coordinates.
Output ONLY a JSON object, no explanations, in this format:
{"questions": [{"q": "Question 1", "question_text": "...", "reference_solution": "...", "max_score": 5, "criteria": "...", "graph": {...}}, ...]}
If you cannot determine a max score for a question, assign a reasonable default based on the question type and note "Max score set by AI default — please confirm" in criteria.
If the photos yield no questions at all, output {"questions": []}.
"""

VALIDATE_SYSTEM = """You are a careful, independent solver and checker. You are given a question and a teacher's reference solution that was transcribed from a photo (the transcription may contain errors).
Your job:
1. Solve the question yourself, step by step, WITHOUT trusting the reference solution.
2. Compare your final answer with the teacher's reference solution.
3. If the question is a numerical computation, also provide a single Python expression (using only arithmetic operators and the math module, e.g. "math.pi * 3**2") that computes the answer. Leave it empty if the question is not numerical.
Output ONLY a JSON object, no explanations, in this format:
{"your_answer": "<your final answer, concise>", "agrees": true/false, "confidence": "high/medium/low", "note": "<one sentence on any discrepancy, or empty>", "python_check": "<expression or empty>"}
Set "agrees" to true only if you are confident the two answers match (allowing for equivalent forms, e.g. 0.5 = 1/2). If the reference solution is missing, garbled, or you cannot solve the question, set "agrees" to false and explain in "note".
"""

GRADER_SYSTEM = """You are a strict grader who scores exactly by the rubric. Rules:
1. Score ONLY what you can actually see in the student's paper photos. If a question is missing, illegible, or unanswered, give it 0 and note why in feedback.
2. No question's score may exceed its max score.
3. First briefly describe the student answer you detected for each question (quote or summarize it), then score it against the rubric — this reduces hallucination.
4. Output ONLY a JSON object, no explanations, in this format:
{"questions": [{"q": "question label", "score": numeric score, "max_score": numeric max, "answer_seen": "brief description of the detected student answer", "feedback": "reason for the score/deductions"}],
 "total": numeric total, "max_total": numeric max total, "overall_feedback": "overall comment"}
total must equal the sum of the question scores, and max_total must equal the sum of the max scores.
"""


def _get_client():
    try:
        from openai import OpenAI
    except ImportError as e:
        raise RuntimeError("openai SDK is missing. Run: pip install -r requirements.txt") from e
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured. Please enter your API key on the Settings page.")
    return OpenAI(api_key=api_key)


def _parse_json_loose(text):
    """Parse model JSON output: try a direct parse, then fall back to stripping
    markdown fences / extracting the outermost braces."""
    text = (text or "").strip()
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass
    m = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if m:
        text = m.group(1).strip()
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return json.loads(text[start : end + 1])
    raise ValueError("The model output is not valid JSON")


def _data_url(path):
    ext = os.path.splitext(path)[1].lower()
    media_type = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }.get(ext, "image/jpeg")
    with open(path, "rb") as f:
        data = base64.standard_b64encode(f.read()).decode("utf-8")
    return f"data:{media_type};base64,{data}"


def _chat_json(system, user_content, max_tokens):
    client = _get_client()
    resp = client.chat.completions.create(
        model=DEFAULT_MODEL,
        max_tokens=max_tokens,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
    )
    text = (resp.choices[0].message.content or "").strip()
    if not text:
        raise ValueError("The model returned an empty response. Please try again.")
    return _parse_json_loose(text)


def _digits_of(text):
    """Extract float numbers from a string for numeric comparison."""
    return [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", text or "")]


def normalize_latex(text):
    """Normalize LaTeX math delimiters in digitized/AI-generated text.

    The vision model often emits LaTeX with inconsistent $...$ delimiters and
    uses \\\\ as a formula separator, which leaves fragments like \\frac{5}{n}
    unrendered by KaTeX. This pass:
    1. Keeps already-delimited $...$ / $$...$$ spans untouched.
    2. Splits the rest on \\\\ separators (which become line breaks); wraps
       any segment containing a LaTeX command (\\<letter>) in $...$.
    3. Plain English without LaTeX commands passes through unchanged.
    """
    if not text:
        return text
    tokens = re.split(r"(\$\$.+?\$\$|\$.+?\$)", text, flags=re.DOTALL)
    segs = []  # strings; "\n" marks a hard break from a \\ separator
    for tok in tokens:
        if not tok:
            continue
        if len(tok) >= 2 and tok.startswith("$") and tok.endswith("$"):
            segs.append(tok)  # already-delimited math, keep as-is
            continue
        for part in re.split(r"(\\\\)", tok):
            if part == "\\\\":
                segs.append("\n")
                continue
            part = part.strip(" \t")
            if not part:
                continue
            if re.search(r"\\[a-zA-Z%$&#_{}]", part):
                part = "$" + part.replace("$", "") + "$"
            segs.append(part)
    result = ""
    for s in segs:
        if s == "\n":
            result = result.rstrip(" ") + "\n"
        else:
            if result and not result.endswith("\n"):
                result += " "
            result += s
    return result.strip(" ")
    """Extract float numbers from a string for numeric comparison."""
    return [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", text or "")]


def _safe_eval(expr, extra_names=None):
    """Evaluate a pure-arithmetic Python expression safely.

    Only numbers, arithmetic operators, whitelisted math functions, and
    `extra_names` (e.g. {"x": value}) are allowed — no imports, attribute
    access, or other names. Returns the float result, or None if disallowed.
    """
    import ast
    import math

    allowed_names = {
        "pi": math.pi, "e": math.e, "tau": math.tau,
        "sqrt": math.sqrt, "sin": math.sin, "cos": math.cos, "tan": math.tan,
        "log": math.log, "log10": math.log10, "exp": math.exp,
        "abs": abs, "round": round, "pow": pow, "min": min, "max": max,
    }
    if extra_names:
        allowed_names = dict(allowed_names, **extra_names)
    allowed_nodes = (
        ast.Expression, ast.BinOp, ast.UnaryOp, ast.Call, ast.Name, ast.Load,
        ast.Constant, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.Mod,
        ast.USub, ast.UAdd, ast.FloorDiv,
    )
    try:
        tree = ast.parse(expr, mode="eval")
    except (SyntaxError, ValueError):
        return None
    for node in ast.walk(tree):
        if not isinstance(node, allowed_nodes):
            return None
        if isinstance(node, ast.Name) and node.id not in allowed_names:
            return None
        if isinstance(node, ast.Call) and not isinstance(node.func, ast.Name):
            return None
    try:
        return float(eval(compile(tree, "<check>", "eval"), {"__builtins__": {}}, allowed_names))  # noqa: S307
    except Exception:
        return None


def _sample_function(expr, x_min, x_max, n=200):
    """Sample y = f(x) for a function-graph spec. Returns
    {"points": [[x, y], ...], "x_min", "x_max", "y_min", "y_max"} or None."""
    import math

    try:
        x_min, x_max = float(x_min), float(x_max)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(x_min) and math.isfinite(x_max)) or x_max <= x_min:
        return None
    points = []
    for i in range(n + 1):
        x = x_min + (x_max - x_min) * i / n
        y = _safe_eval(expr, {"x": x})
        if y is not None and math.isfinite(y) and abs(y) < 1e9:
            points.append([round(x, 4), round(y, 4)])
    if len(points) < 10:
        return None
    ys = [p[1] for p in points]
    y_min, y_max = min(ys), max(ys)
    pad = max((y_max - y_min) * 0.1, 1.0)
    return {
        "kind": "function",
        "points": points,
        "x_min": x_min, "x_max": x_max,
        "y_min": y_min - pad, "y_max": y_max + pad,
        "expression": expr,
    }


def _crop_graph(image_paths, spec, upload_dir):
    """Crop a diagram region from an example photo. `spec` carries image_index
    and 0-1000 relative box coords. Returns {"kind": "image", "url": ...} or None."""
    from PIL import Image

    if not upload_dir:
        return None
    try:
        idx = int(spec.get("image_index", 0))
        src = image_paths[idx]
        x0, y0 = float(spec.get("x_min", 0)), float(spec.get("y_min", 0))
        x1, y1 = float(spec.get("x_max", 1000)), float(spec.get("y_max", 1000))
    except (TypeError, ValueError, IndexError):
        return None
    if not (0 <= x0 < x1 <= 1000 and 0 <= y0 < y1 <= 1000):
        return None
    try:
        im = Image.open(src).convert("RGB")
        w, h = im.size
        box = (int(w * x0 / 1000), int(h * y0 / 1000), int(w * x1 / 1000), int(h * y1 / 1000))
        if box[2] - box[0] < 20 or box[3] - box[1] < 20:
            return None
        import random
        import time
        name = f"graph_crop_{int(time.time())}_{random.randint(1000, 9999)}.png"
        im.crop(box).save(os.path.join(upload_dir, name))
        return {"kind": "image", "url": f"/uploads/{name}"}
    except Exception:
        return None


def _process_graph(question, image_paths, upload_dir):
    """Turn a digitized graph spec into render-ready data (or None)."""
    spec = question.get("graph")
    if not isinstance(spec, dict):
        return None
    kind = spec.get("kind")
    if kind == "function":
        expr = str(spec.get("expression", "")).strip()
        if not expr:
            return None
        return _sample_function(expr, spec.get("x_min", -10), spec.get("x_max", 10))
    if kind == "crop":
        return _crop_graph(image_paths, spec, upload_dir)
    return None


def digitize_example(image_paths, hint=None):
    """Vision pass: transcribe the example into per-question data.

    Returns a list of {q, question_text, reference_solution, max_score, criteria}.
    `hint` (optional) is appended when retrying, e.g. to flag a misread solution.
    """
    if not image_paths:
        raise RuntimeError("No example photos received. Please take or upload a reference example first.")
    text = "These photos show the teacher's reference example (a hand-graded paper, answer key, or scoring guide). Digitize every question, its reference solution, and the grading rubric."
    if hint:
        text += f"\n\nIMPORTANT CORRECTION HINT for this retry: {hint} Look at the photos again very carefully and fix the transcription."
    user_content = [{"type": "text", "text": text}]
    for p in image_paths:
        user_content.append({"type": "image_url", "image_url": {"url": _data_url(p)}})
    data = _chat_json(EXAMPLE_RUBRIC_SYSTEM, user_content, max_tokens=4096)
    questions = []
    for it in data.get("questions", []):
        try:
            ms = float(it.get("max_score", 0))
        except (TypeError, ValueError):
            ms = 0
        questions.append(
            {
                "q": str(it.get("q", "")).strip(),
                "question_text": normalize_latex(str(it.get("question_text", "")).strip()),
                "reference_solution": normalize_latex(str(it.get("reference_solution", "")).strip()),
                "max_score": ms,
                "criteria": normalize_latex(str(it.get("criteria", "")).strip()),
                "graph": it.get("graph") if isinstance(it.get("graph"), dict) else None,
            }
        )
    return questions


def validate_question(question_text, reference_solution):
    """Independently re-solve a question and compare with the reference solution.

    Returns {"verdict": "confirmed"|"mismatch"|"unsure", "note": str}.
    Uses an LLM re-solve plus a safe Python numeric check when applicable.
    """
    if not (question_text or "").strip() or not (reference_solution or "").strip():
        return {"verdict": "unsure", "note": "No question text or reference solution to validate."}
    data = _chat_json(
        VALIDATE_SYSTEM,
        f"Question:\n{question_text}\n\nTeacher's reference solution (transcribed from photo):\n{reference_solution}",
        max_tokens=2048,
    )
    agrees = data.get("agrees")
    confidence = str(data.get("confidence", "")).lower()
    note = str(data.get("note", "")).strip()
    your_answer = str(data.get("your_answer", "")).strip()

    # Strong check: evaluate the model's python expression and compare numerically.
    py_expr = str(data.get("python_check", "")).strip()
    if py_expr:
        computed = _safe_eval(py_expr)
        ref_nums = _digits_of(reference_solution)
        if computed is not None and ref_nums:
            if any(abs(computed - n) <= max(1e-6, abs(n) * 1e-4) for n in ref_nums):
                return {"verdict": "confirmed", "note": f"Python check passed ({py_expr} = {computed:g})."}
            return {
                "verdict": "mismatch",
                "note": f"Python check: {py_expr} = {computed:g}, but the reference solution says {reference_solution!r}.",
            }

    if agrees is True and confidence in ("high", "medium"):
        detail = f" Independent solution: {your_answer}." if your_answer else ""
        return {"verdict": "confirmed", "note": (note + detail).strip()}
    if agrees is False:
        detail = f" Independent solution: {your_answer}." if your_answer else ""
        return {"verdict": "mismatch", "note": (note + detail).strip() or "Independent solving disagrees with the reference solution."}
    return {"verdict": "unsure", "note": note or "Could not confidently validate the reference solution."}


def build_example_rubric(image_paths, upload_dir=None, max_workers=5):
    """Full pipeline: digitize the example, validate each reference solution
    (independent re-solve + Python numeric check), retry failed questions once
    with a correction hint, and flag anything still unresolved for the teacher.

    Returns {"items": [...]} where each item has q, question_text,
    reference_solution, max_score, criteria, verified ("yes"|"review"),
    verify_note, and graph (render-ready function plot / cropped image / None).
    """
    from concurrent.futures import ThreadPoolExecutor

    questions = digitize_example(image_paths)

    def _validate_one(q):
        return validate_question(q.get("question_text", ""), q.get("reference_solution", ""))

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        verdicts = list(pool.map(_validate_one, questions))

    # Retry the failures once: re-digitize with a correction hint, then re-validate.
    failed_idx = [i for i, v in enumerate(verdicts) if v["verdict"] != "confirmed"]
    if failed_idx:
        hint_bits = []
        for i in failed_idx:
            q = questions[i]
            hint_bits.append(
                f'{q["q"]}: you transcribed the solution as "{q["reference_solution"]}", '
                f'but validation says: {verdicts[i]["note"]}'
            )
        hint = "Some transcriptions failed validation:\n" + "\n".join(hint_bits)
        questions = digitize_example(image_paths, hint=hint)
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            verdicts = list(pool.map(_validate_one, questions))

    items = []
    for q, v in zip(questions, verdicts):
        items.append(
            {
                "q": q["q"],
                "question_text": q["question_text"],
                "reference_solution": q["reference_solution"],
                "max_score": q["max_score"],
                "criteria": q["criteria"],
                "verified": "yes" if v["verdict"] == "confirmed" else "review",
                "verify_note": "" if v["verdict"] == "confirmed" else v["note"],
                "graph": _process_graph(q, image_paths, upload_dir),
            }
        )
    return {"items": items}


def rubric_from_example(image_paths, upload_dir=None):
    """Legacy alias: build the full digitize+validate rubric from example photos."""
    return build_example_rubric(image_paths, upload_dir=upload_dir)


def rubric_items_to_text(items):
    """Render the structured rubric as plain text (for teacher review / archiving)."""
    lines = []
    for it in items:
        ms = it.get("max_score", 0)
        ms_s = int(ms) if float(ms).is_integer() else ms
        lines.append(f"{it.get('q', '')} (max {ms_s} pts): {it.get('criteria', '')}")
        sol = (it.get("reference_solution") or "").strip()
        if sol:
            lines.append(f"  Reference solution: {sol}")
    return "\n".join(lines)


def build_rubric_prompt_text(rubric_text, rubric_items):
    """Rubric text used for grading: prefer the structured version (more accurate)."""
    if rubric_items:
        # Self-heal: normalize LaTeX in rubrics saved before the fix.
        for it in rubric_items:
            for k in ("question_text", "reference_solution", "criteria"):
                if it.get(k):
                    it[k] = normalize_latex(it[k])
        return rubric_items_to_text(rubric_items)
    return rubric_text or ""


def grade_paper(rubric_text, rubric_items, image_paths, reference_image_paths=None):
    """Grade a paper. Returns {"questions": [...], "total": x, "max_total": y, "overall_feedback": ...}."""
    if not image_paths:
        raise RuntimeError("No paper photos received. Please take/upload photos first.")
    rubric_block = build_rubric_prompt_text(rubric_text, rubric_items)
    if not rubric_block.strip():
        raise RuntimeError("No rubric has been set for this assignment. Please enter the grading rules on the assignment setup page first.")

    user_content = []
    for p in reference_image_paths or []:
        user_content.append(
            {"type": "image_url", "image_url": {"url": _data_url(p)}}
        )
    ref_note = "Above are the reference/answer-key images; " if reference_image_paths else ""
    user_content.append(
        {
            "type": "text",
            "text": f"{ref_note}below are photos of the student's paper. Grade each question according to this rubric:\n\n{rubric_block}",
        }
    )
    for p in image_paths:
        user_content.append(
            {"type": "image_url", "image_url": {"url": _data_url(p)}}
        )

    data = _chat_json(GRADER_SYSTEM, user_content, max_tokens=4096)

    # Normalize + safety checks.
    questions = []
    for q in data.get("questions", []):
        try:
            max_s = float(q.get("max_score", 0))
        except (TypeError, ValueError):
            max_s = 0
        try:
            s = float(q.get("score", 0))
        except (TypeError, ValueError):
            s = 0
        s = max(0, min(s, max_s))  # clamp: never above max, never negative
        questions.append(
            {
                "q": str(q.get("q", "")),
                "score": s,
                "max_score": max_s,
                "answer_seen": normalize_latex(str(q.get("answer_seen", ""))),
                "feedback": normalize_latex(str(q.get("feedback", ""))),
            }
        )
    total = round(sum(q["score"] for q in questions), 2)
    max_total = round(sum(q["max_score"] for q in questions), 2)
    return {
        "questions": questions,
        "total": total,
        "max_total": max_total,
        "overall_feedback": normalize_latex(str(data.get("overall_feedback", ""))),
    }
