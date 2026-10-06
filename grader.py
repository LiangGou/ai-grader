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
- "question_text": the question itself, transcribed as faithfully as you can.
- "reference_solution": the teacher's correct solution / working / final answer, transcribed faithfully. Capture the final answer at minimum; include key working steps if shown.
- "max_score": the max points for the question.
- "criteria": the scoring/deduction rules you can infer from the teacher's markings (check marks, crosses, circled mistakes, handwritten notes like "-2"). For example, if the teacher wrote "-2" next to a missing unit, include "deduct 2 points for a missing/wrong unit".
Output ONLY a JSON object, no explanations, in this format:
{"questions": [{"q": "Question 1", "question_text": "...", "reference_solution": "...", "max_score": 5, "criteria": "..."}, ...]}
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


def _safe_eval(expr):
    """Evaluate a pure-arithmetic Python expression safely.

    Only numbers, arithmetic operators, and whitelisted math functions are
    allowed — no imports, attribute access, or names beyond the whitelist.
    Returns the float result, or None if the expression is not allowed.
    """
    import ast
    import math

    allowed_names = {
        "pi": math.pi, "e": math.e, "tau": math.tau,
        "sqrt": math.sqrt, "sin": math.sin, "cos": math.cos, "tan": math.tan,
        "log": math.log, "log10": math.log10, "exp": math.exp,
        "abs": abs, "round": round, "pow": pow, "min": min, "max": max,
    }
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
                "question_text": str(it.get("question_text", "")).strip(),
                "reference_solution": str(it.get("reference_solution", "")).strip(),
                "max_score": ms,
                "criteria": str(it.get("criteria", "")).strip(),
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


def build_example_rubric(image_paths, max_workers=5):
    """Full pipeline: digitize the example, validate each reference solution
    (independent re-solve + Python numeric check), retry failed questions once
    with a correction hint, and flag anything still unresolved for the teacher.

    Returns {"items": [...]} where each item has q, question_text,
    reference_solution, max_score, criteria, verified ("yes"|"review"),
    and verify_note.
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
            }
        )
    return {"items": items}


def rubric_from_example(image_paths):
    """Legacy alias: build the full digitize+validate rubric from example photos."""
    return build_example_rubric(image_paths)


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
                "answer_seen": str(q.get("answer_seen", "")),
                "feedback": str(q.get("feedback", "")),
            }
        )
    total = round(sum(q["score"] for q in questions), 2)
    max_total = round(sum(q["max_score"] for q in questions), 2)
    return {
        "questions": questions,
        "total": total,
        "max_total": max_total,
        "overall_feedback": str(data.get("overall_feedback", "")),
    }
