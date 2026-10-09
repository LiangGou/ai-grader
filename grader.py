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
import tempfile

from dotenv import load_dotenv

load_dotenv()

DEFAULT_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o")
# Reasoning model for hard tasks (diagram code generation, complex math).
# Simple tasks (transcription) use DEFAULT_MODEL to save cost/time.
REASONING_MODEL = os.environ.get("OPENAI_REASONING_MODEL", "o3")

EXAMPLE_RUBRIC_SYSTEM = """You are digitizing a teacher's reference example (a hand-graded paper, answer key, or scoring guide) into structured data.
For each question, transcribe and extract:
- "q": the question label, use short format like "Q1", "Q2", "Q3" (not "Question 1").
- "question_text": the question itself, transcribed faithfully. Write ALL mathematics in LaTeX: use $...$ for inline math (e.g. $2x + 6 = 14$) and $$...$$ for display math (e.g. $$\\int_0^1 x^2 dx$$). EVERY math fragment MUST be wrapped in $ delimiters — never leave LaTeX commands like \\frac outside $...$. NEVER use \\[...\\] or \\(...\\) delimiters, only $...$ and $$...$$.
- "reference_solution": the teacher's correct solution / worked example / answer key, IF SHOWN in the photos (printed answer key, teacher\'s handwritten worked solutions, or graded examples with correct working). Transcribe faithfully with mathematics in LaTeX as above. IMPORTANT: if the photos show ONLY blank questions with no solutions, answer key, or worked examples, leave this EMPTY (""). Do NOT invent a solution. Do NOT copy student scratch work as the reference solution — only the teacher\'s authoritative solution.
- "max_score": the max points for the question, as PRINTED on the paper (the teacher assigns this — look for "(5 pts)", "[10]", "5 points", etc.).
- "criteria": the scoring/deduction rules you can infer from the teacher's markings (check marks, crosses, circled mistakes, handwritten notes like "-2"). For example, if the teacher wrote "-2" next to a missing unit, include "deduct 2 points for a missing/wrong unit".
- "has_diagram": true or false — REQUIRED. Look carefully at the example photo: does this question show ANY diagram, graph, plot, coordinate system, or geometric figure? You must answer this for every question.
- "diagram_hint": REQUIRED when has_diagram is true, omitted otherwise. One sentence describing what the diagram shows (e.g. "graph of a rational function with vertical asymptotes and a hole", "bar chart of test scores", "midpoint rectangles under a curve"). This helps regenerate the diagram accurately.
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
3. The rubric criteria name specific STEPS (e.g. "Step 1: setup", "Part (b) Step 2: computation"). Award each point ONLY if the student's paper shows that step done correctly — check step by step.
4. Follow-through credit: an arithmetic slip in one step does not forfeit later steps if the student's method stays correct.
5. Any mathematically valid method earns full credit, even if the student's steps differ from the reference solution's steps — match by mathematical content, not by step order.
6. First briefly describe the student answer you detected for each question (quote or summarize it), then score it step-by-step against the rubric — this reduces hallucination.
7. Output ONLY a JSON object, no explanations, in this format:
{"questions": [{"q": "question label", "score": numeric score, "max_score": numeric max, "answer_seen": "brief description of the detected student answer", "feedback": "reason for the score/deductions, naming which steps earned/lost points"}],
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
    # Fail fast instead of hanging: the default SDK timeout is 10 minutes, which
    # would leave a background job stuck on "Reading questions…" indefinitely.
    return OpenAI(api_key=api_key, timeout=120.0)


def _parse_json_loose(text):
    """Parse model JSON output: try a direct parse, then fall back to stripping
    markdown fences / extracting the outermost braces."""
    text = (text or "").strip()
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass
    # Try EVERY code fence (not just the first) — the model might put a
    # ```python block before the ```json block. Found by claude-opus-5-5
    # review 2026-10-07.
    for m in re.finditer(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL):
        try:
            return json.loads(m.group(1).strip())
        except (json.JSONDecodeError, TypeError):
            continue
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return json.loads(text[start : end + 1])
    raise ValueError("The model output is not valid JSON")



def _open_image(image_path):
    """Open an image from filesystem path or db: prefix (Postgres mode)."""
    if image_path.startswith("db:"):
        import db as _db
        import io
        data, _ = _db.get_image(image_path[3:])
        if data is None:
            raise FileNotFoundError(f"Image not found in DB: {image_path}")
        return Image.open(io.BytesIO(data)).convert("RGB")
    return Image.open(image_path).convert("RGB")

def _data_url(path):
    # Handle db: prefix (Postgres mode: images stored in DB)
    if path.startswith("db:"):
        import db as _db
        data_bytes, mime = _db.get_image(path[3:])
        if data_bytes is None:
            raise FileNotFoundError(f"Image not found in DB: {path}")
        data = base64.standard_b64encode(data_bytes).decode("utf-8")
        return f"data:{mime or 'image/jpeg'};base64,{data}"
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


# Optional hook: set by app.py to log every OpenAI call for cost tracking.
# Signature: fn(endpoint_label, model, prompt_tokens, completion_tokens).
usage_logger = None
# Optional hook: set by app.py; returns True if the call may proceed.
# Signature: fn() -> bool. Used to enforce a daily token budget.
budget_checker = None
# Label for the current high-level operation (set by callers, e.g. "solve_rubric").
current_operation = "general"


def _chat_json(system, user_content, max_tokens, model=None, reasoning_effort=None, _raw=None):
    client = _get_client()
    model = model or DEFAULT_MODEL
    # Reasoning models (o1/o3) and gpt-5.x use max_completion_tokens and don't
    # support response_format=json_object; ask for JSON in the prompt instead.
    is_reasoning = (model.startswith("o1") or model.startswith("o3")
                    or model.startswith("gpt-5"))
    kwargs = dict(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
    )
    if is_reasoning:
        kwargs["max_completion_tokens"] = max_tokens
        if reasoning_effort:
            kwargs["reasoning_effort"] = reasoning_effort
    else:
        kwargs["max_tokens"] = max_tokens
        kwargs["response_format"] = {"type": "json_object"}
    if budget_checker is not None and not budget_checker():
        raise RuntimeError("Daily API budget exceeded — please try again tomorrow.")
    resp = client.chat.completions.create(**kwargs)
    if usage_logger is not None:
        try:
            u = getattr(resp, "usage", None)
            usage_logger(current_operation, model,
                         getattr(u, "prompt_tokens", 0) if u else 0,
                         getattr(u, "completion_tokens", 0) if u else 0)
        except Exception:
            pass
    text = (resp.choices[0].message.content or "").strip()
    if _raw is not None:
        _raw.append({"model": model, "effort": reasoning_effort, "raw": text})
    if not text:
        raise ValueError("The model returned an empty response. Please try again.")
    return _parse_json_loose(text)


def _digits_of(text):
    """Extract float numbers from a string for numeric comparison."""
    return [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", text or "")]


def _balance_braces(s):
    """Repair unbalanced { } in a LaTeX math fragment.

    The model sometimes emits stray } (e.g. "(slope}=0)") or unclosed {,
    which makes KaTeX fail and render red. This drops } with no matching {
    and appends } for unclosed {. Escaped \\{ \\} are literal braces and
    don't affect the count. Valid LaTeX passes through unchanged.
    """
    result = []
    depth = 0
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if ch == "\\" and i + 1 < n:
            # Consume backslash + next char together. After a backslash the
            # next char is never a structural brace: \{ \} are literals,
            # \\ is a line break, \f etc. are commands.
            # Found by claude-opus-5-5 review 2026-10-07.
            result.append(s[i:i+2])
            i += 2
            continue
        if ch == "{":
            depth += 1
            result.append(ch)
        elif ch == "}":
            if depth > 0:
                depth -= 1
                result.append(ch)
            # else: drop unmatched }
        else:
            result.append(ch)
        i += 1
    result.append("}" * depth)
    return "".join(result)


def normalize_latex(text):
    r"""Normalize LaTeX math delimiters in digitized/AI-generated text.

    The vision model emits LaTeX with inconsistent delimiters: bare fragments,
    \\\\-separated formulas, and mixed forms like $\\[...\\]$. Any of these
    leave KaTeX unable to render (or worse, mis-pair $ delimiters so a huge
    span fails to parse and shows red). This pass:
    1. Fixes mixed delimiters like $\[...\]$: drops a single $ hugging
       \[ \] \( \), then converts \[...\] -> $$...$$ and \(...\) -> $...$,
       so they can't mis-pair into a broken $...$ span.
    2. Drops a stray lone $ (odd count): auto-render would pair it with a
       far-away $ and swallow everything between into a broken math span.
    3. Keeps already-delimited $...$ / $$...$$ spans untouched.
    4. Splits the rest on \\\\ separators; wraps any segment containing a
       LaTeX command (\\<letter>) in $...$.
    Plain English without LaTeX commands passes through unchanged.
    """
    if not text:
        return text
    # The model sometimes emits invalid Unicode (U+FFFD replacement chars),
    # e.g. corrupted circled digits like �1� (meant ①). Recover the digit,
    # drop any remaining replacement chars so they don't render as mojibake.
    text = re.sub(r"\ufffd(\d+)\ufffd", r"\1", text)
    text = text.replace("\ufffd", "")
    # The model also emits intact circled digits ①-⑳ (U+2460-U+2473) etc.,
    # which KaTeX cannot render (shows as �). Convert to plain digits.
    def _circled_to_digit(m):
        cp = ord(m.group(0))
        if 0x2460 <= cp <= 0x2473:  # ①-⑳
            return str(cp - 0x2460 + 1)
        if 0x3251 <= cp <= 0x325F:  # ㉑-㉟
            return str(cp - 0x3251 + 21)
        if 0x32B1 <= cp <= 0x32BF:  # ㊱-㊿
            return str(cp - 0x32B1 + 36)
        return m.group(0)
    text = re.sub("[\u2460-\u2473\u3251-\u325f\u32b1-\u32bf]", _circled_to_digit, text)
    # The model types literal \n (backslash + n) for line breaks between
    # formulas. KaTeX has no \n command, so it renders red. Turn it into a
    # real newline — but NOT when the 'n' starts a real LaTeX command like
    # \neq, \nabla. The regex consumes '\n'; the lookahead checks the
    # remainder with a word boundary (so "\nunder" -> newline, not \nu).
    # Found by claude-opus-5-5 2026-10-07.
    _N_CMD_REST = r"(?:eq|abla|u|ot|i|eg|earrow|warrow|mid|parallel|less|gtr|leq|geq|sim|cong|vdash|vDash|Vdash|VDash|exists|in|otin)"
    text = re.sub(r"\\n(?!" + _N_CMD_REST + r"\b)", "\n", text)
    # The model sometimes mixes delimiters like $\[...\]$ or $\(...\)$.
    # Drop a single $ hugging \[ \] \( \) first (a $$ pair is left alone),
    # then convert \[...\] -> $$...$$ and \(...\) -> $...$.
    text = re.sub(r"(?<!\$)\$(?=\\\[)", "", text)
    text = re.sub(r"(?<=\\\])\$(?!\$)", "", text)
    text = re.sub(r"(?<!\\)\\\[(.+?)(?<!\\)\\\]", r"$$\1$$", text, flags=re.DOTALL)
    text = re.sub(r"(?<!\$)\$(?=\\\()", "", text)
    text = re.sub(r"(?<=\\\))\$(?!\$)", "", text)
    text = re.sub(r"(?<!\\)\\\((.+?)(?<!\\)\\\)", r"$\1$", text, flags=re.DOTALL)
    spans = []

    def _save(m):
        body = m.group(0)
        # Balance braces inside $$...$$ (keep the delimiters).
        if body.startswith("$$") and body.endswith("$$") and len(body) >= 4:
            body = "$$" + _balance_braces(body[2:-2]) + "$$"
        spans.append(body)
        return "\x00%d\x00" % len(spans)

    # Protect \begin{env}...\end{env} blocks FIRST: they must stay together as
    # one math unit. Splitting on \\ would shatter alignment environments
    # (aligned, align, cases, ...) into unrenderable fragments.
    def _save_env(m):
        body = m.group(0)
        # If not already inside math delimiters, wrap as display math.
        if body.lstrip().startswith("$$") or (
            body.lstrip().startswith("$") and not body.lstrip().startswith("$$")
        ):
            pass  # already delimited, keep as-is
        else:
            body = "$$" + body.strip() + "$$"
        # Balance braces inside the math (preserve the $$ delimiters).
        if body.startswith("$$") and body.endswith("$$") and len(body) >= 4:
            body = "$$" + _balance_braces(body[2:-2]) + "$$"
        spans.append(body)
        return "\x00%d\x00" % len(spans)

    text = re.sub(r"(\${1,2})?\\begin\{[a-zA-Z*]+\}.*?\\end\{[a-zA-Z*]+\}(\${1,2})?",
                  _save_env, text, flags=re.DOTALL)

    protected = re.sub(r"\$\$.+?\$\$", _save, text, flags=re.DOTALL)
    positions = [m.start() for m in re.finditer(r"(?<!\\)\$", protected)]
    if len(positions) % 2 == 1:
        i = positions[-1]
        protected = protected[:i] + protected[i + 1 :]
    for i, s in enumerate(spans, start=1):
        protected = protected.replace("\x00%d\x00" % i, s)
    text = protected
    tokens = re.split(r"(\$\$.+?\$\$|\$.+?\$)", text, flags=re.DOTALL)
    segs = []  # strings; "\n" marks a hard break from a \\ separator
    for tok in tokens:
        if not tok:
            continue
        if len(tok) >= 2 and tok.startswith("$") and tok.endswith("$"):
            # Already-delimited math: balance braces inside, keep delimiters.
            if tok.startswith("$$") and tok.endswith("$$") and len(tok) >= 4:
                segs.append("$$" + _balance_braces(tok[2:-2]) + "$$")
            elif len(tok) >= 2:
                segs.append("$" + _balance_braces(tok[1:-1]) + "$")
            else:
                segs.append(tok)
            continue
        for part in re.split(r"(\\\\)", tok):
            if part == "\\\\":
                segs.append("\n")
                continue
            part = part.strip(" \t")
            if not part:
                continue
            if re.search(r"\\[a-zA-Z%$&#_{}]", part):
                part = "$" + _balance_braces(part.replace("$", "")) + "$"
            segs.append(part)
    result = ""
    for s in segs:
        if s == "\n":
            result = result.rstrip(" ") + "\n"
        else:
            # Don't add space before punctuation (.,;:!?) to avoid "word ."
            if result and not result.endswith("\n") and not re.match(r"^[.,;:!?)]", s):
                result += " "
            # Balance braces in non-math segments too (e.g. "(slope}=0)").
            if not (s.startswith("$") and s.endswith("$") and len(s) >= 2):
                s = _balance_braces(s)
            result += s
    return result.strip(" ")
    """Extract float numbers from a string for numeric comparison."""
    return [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", text or "")]


def _safe_eval(expr, extra_names=None):
    """Evaluate a pure-arithmetic Python expression safely.

    Only numbers, arithmetic operators, whitelisted math functions, and
    `extra_names` (e.g. {"x": value}) are allowed — no imports, attribute
    access, or other names. Returns the float result, or None if disallowed.

    Also handles "LHS = RHS" equality format (common in LLM-generated checks):
    returns LHS - RHS, so ~0 means the equality holds.
    """
    import ast
    import math

    # Handle "expr = value" format: verify the equality by subtraction.
    # (Bug fix 2026-10-07: LLM generates "6*120-2 = 718", not just "6*120-2".)
    if isinstance(expr, str) and "=" in expr and "==" not in expr:
        parts = expr.split("=", 1)
        if len(parts) == 2 and parts[0].strip() and parts[1].strip():
            lhs = _safe_eval(parts[0].strip(), extra_names)
            rhs = _safe_eval(parts[1].strip(), extra_names)
            if lhs is not None and rhs is not None:
                return lhs - rhs
            return None

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
        # DoS guard: only numeric constants (no str/bytes that Mult could
        # blow up, no bool). Found by claude-opus-5-5 review 2026-10-07.
        if isinstance(node, ast.Constant) and not isinstance(node.value, (int, float)):
            return None
        # DoS guard: bound Pow exponents (9**9**9**9 hangs forever).
        # Only allow literal numeric exponents; nested power towers like
        # 9**(9**9) have a BinOp exponent and are rejected.
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
            exp = node.right
            if not (isinstance(exp, ast.Constant)
                    and isinstance(exp.value, (int, float))
                    and abs(exp.value) <= 100):
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


def _localize_diagram(image_path, question_text):
    """Locate a diagram's bounding box using grid-overlay visual prompting.

    Overlays a labeled 0-1000 coordinate grid on the photo and makes a
    dedicated vision call whose ONLY job is localization (far more accurate
    than asking for coordinates in the busy digitization pass).
    Returns (x_min, y_min, x_max, y_max) in 0-1000 coords, or None.
    """
    from PIL import Image, ImageDraw

    try:
        im = _open_image(image_path)
    except Exception:
        return None
    w, h = im.size
    grid = im.copy()
    draw = ImageDraw.Draw(grid, "RGBA")
    # Grid lines every 100 units on the 0-1000 scale.
    for v in range(0, 1001, 100):
        x = int(w * v / 1000)
        y = int(h * v / 1000)
        draw.line([(x, 0), (x, h)], fill=(255, 0, 0, 160), width=max(1, w // 800))
        draw.line([(0, y), (w, y)], fill=(255, 0, 0, 160), width=max(1, h // 800))
        # Labels along the top and left edges.
        for tx, ty in ((x + 3, 3), (3, y + 3)):
            draw.rectangle([tx, ty, tx + 44, ty + 20], fill=(255, 255, 255, 220))
            draw.text((tx + 4, ty + 2), str(v), fill=(200, 0, 0, 255))
    tmp = os.path.join(tempfile.gettempdir(), f"grid_{os.path.basename(image_path)}")
    try:
        grid.save(tmp)
    except Exception:
        return None

    system = (
        "You locate diagrams in photos of math worksheets. The photo has a RED "
        "coordinate grid overlay: numbers 0-1000 run left-to-right along the top "
        "and top-to-bottom along the left edge. Read coordinates off the grid."
    )
    user_content = [
        {
            "type": "text",
            "text": (
                f"For this question: {question_text}\n\n"
                "Find the DIAGRAM / figure / graph / plot associated with this question. "
                "Ignore all text, handwriting, and other questions' diagrams. "
                "Return ONLY a JSON object with the TIGHT bounding box of just the diagram, "
                "using the grid coordinates: "
                '{"x_min": <left>, "y_min": <top>, "x_max": <right>, "y_max": <bottom>}. '
                "Be tight: exclude surrounding text and whitespace margins."
            ),
        },
        {"type": "image_url", "image_url": {"url": _data_url(tmp)}},
    ]
    try:
        data = _chat_json(system, user_content, max_tokens=256)
        x0, y0 = float(data.get("x_min", 0)), float(data.get("y_min", 0))
        x1, y1 = float(data.get("x_max", 0)), float(data.get("y_max", 0))
    except Exception:
        return None
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass
    if not (0 <= x0 < x1 <= 1000 and 0 <= y0 < y1 <= 1000):
        return None
    # Reject absurd boxes (tiny sliver or nearly the whole photo).
    area = (x1 - x0) * (y1 - y0)
    if area < 2000 or area > 900000:
        return None
    return (x0, y0, x1, y1)


DIAGRAM_GEN_SYSTEM = """You are a math diagram generator. Given a math problem and its solution, you write Python code using matplotlib that draws the diagram/figure for the problem.

Output ONLY a JSON object in exactly this format (no markdown, no extra text):
{"code": "<python code here>"}

If a diagram would NOT help understand this problem (e.g. pure algebra with no geometric meaning), output {"code": ""}.

Rules for the code:
- Start with: import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt; import numpy as np
- Create the figure with a reasonable size: plt.figure(figsize=(8, 6)) or plt.subplots(figsize=(8, 6)). Do NOT create very tall or very wide figures.
- Do NOT define OUTPUT_PATH (it is already defined, just use it).
- Do NOT call plt.savefig() or plt.show(). Just build the figure with plt.figure() / plt.subplots() and draw on it. The system will save it.
- Solve the problem as needed to determine what to draw (find zeros, asymptotes, intercepts, etc. computationally with numpy).
- Draw clean, labeled diagrams: axes with labels, key points marked and labeled, asymptotes as dashed lines, etc.
- Keep the code self-contained. Only use: matplotlib, numpy, math. No file I/O. No network. No os/sys/subprocess.
- The figure should be clear at 800x600 pixels.
"""

# Patterns that must not appear in AI-generated diagram code.
_DIAGRAM_CODE_BLOCKLIST = (
    "os.", "sys.", "subprocess", "__import__", "eval(", "exec(",
    "open(", "input(", "compile(", "globals(", "locals(",
    "socket", "urllib", "requests", "shutil", "pathlib",
    "output_path =", "savefig", "plt.show",
)


DIAGRAM_VALIDATE_SYSTEM = """You are a math diagram checker. You are shown a diagram generated for a math problem, along with the problem text and reference solution.
Check if the diagram correctly represents the problem:
1. Does it show the right type of graph (e.g. parabola vs line vs rational function)?
2. Are key features correct (intercepts, asymptotes, critical points, shape)?
3. Are axes labeled and the diagram readable?
Output ONLY a JSON object: {"valid": true/false, "reason": "<one sentence>"}
Set valid=true only if the diagram is mathematically correct for the problem."""


NAME_EXTRACT_SYSTEM = """You are looking at a photo of a student's paper/homework/exam.
Find the student's name, which is usually handwritten at the top of the page (e.g. "Name: John Smith" or just "John Smith").
Output ONLY a JSON object: {"name": "<student name or empty string>"}
If no name is visible, return {"name": ""}. Do not guess."""


def extract_student_name(image_path):
    """Extract the student's handwritten name from a paper photo.

    Returns the name string (or empty if not found).
    Crops the top 20% of the image (where names are usually written) for
    better recognition of faint handwriting.
    """
    try:
        # Crop top 20% for better name recognition
        from PIL import Image
        import io, base64, tempfile, os
        crop_path = image_path
        tmp_crop = None
        try:
            from PIL import ImageEnhance
            img = Image.open(_open_image(image_path) if image_path.startswith("db:") else image_path)
            w, h = img.size
            # Crop top 20% where name is usually written
            crop = img.crop((0, 0, w, int(h * 0.2)))
            # Enhance contrast to make faint handwriting more visible
            crop = ImageEnhance.Contrast(crop).enhance(2.0)
            crop = ImageEnhance.Brightness(crop).enhance(1.1)
            tmp_crop = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
            tmp_crop.close()
            crop.save(tmp_crop.name, "JPEG", quality=95)
            crop_path = tmp_crop.name
        except Exception:
            pass

        data_url = _data_url(crop_path)
        data = _chat_json(
            NAME_EXTRACT_SYSTEM,
            [
                {"type": "text", "text": "What is the student's name written at the top of this paper? Look carefully, the handwriting may be faint."},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
            max_tokens=100,
            model="gpt-5.6-sol",
        )
        if tmp_crop:
            try:
                os.unlink(tmp_crop.name)
            except Exception:
                pass
        if isinstance(data, dict):
            return str(data.get("name", "")).strip()
        return ""
    except Exception:
        return ""


def _validate_diagram(image_path, question_text, reference_solution):
    """Validate a generated diagram using the vision model.

    Returns (valid: bool, reason: str). The vision model looks at the
    generated image and checks it against the problem requirements.
    """
    try:
        import base64
        with open(image_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode()
        data = _chat_json(
            DIAGRAM_VALIDATE_SYSTEM,
            [
                {"type": "text", "text": f"Problem:\n{question_text}\n\nReference solution:\n{reference_solution}\n\nDoes this diagram correctly represent the problem?"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}"}},
            ],
            max_tokens=500,
            model="gpt-5.6-sol",
        )
        if isinstance(data, dict):
            return bool(data.get("valid", False)), str(data.get("reason", ""))
        return False, "validation failed"
    except Exception as e:
        return False, f"validation error: {e}"


def _generate_diagram(question_text, reference_solution, upload_dir, diagram_hint=""):
    """Generate a diagram with Python/matplotlib via the reasoning model.

    The reasoning model solves the problem and writes code that draws the
    diagram. The code runs sandboxed (blocklist + subprocess timeout).
    Returns {"kind": "image", "url": ...} or None.
    """
    if not upload_dir:
        return None
    hint_line = f"Diagram description: {diagram_hint}\n\n" if diagram_hint else ""
    prompt = (
        f"Problem:\n{question_text}\n\n"
        f"Reference solution:\n{reference_solution}\n\n"
        f"{hint_line}"
        "Write Python code that draws the diagram for this problem."
    )
    # Retry up to 3 times: gpt-5.6-sol sometimes returns empty responses
    # for complex prompts (seen 2026-10-08 on rational function graphs).
    code = ""
    for attempt in range(3):
        try:
            data = _chat_json(
                DIAGRAM_GEN_SYSTEM,
                [{"type": "text", "text": prompt}],
                max_tokens=2000,
                model="gpt-5.6-sol",  # was o3; gpt-5.6-sol is more reliable (2026-10-08)
            )
            code = data.get("code", "") if isinstance(data, dict) else ""
            if not code or not isinstance(code, str):
                # The model may return the code under a different key or as raw text.
                code = str(data) if data else ""
            # Strip markdown fences if the model added them despite instructions.
            code = re.sub(r"^```(?:python)?\s*", "", code.strip())
            code = re.sub(r"\s*```$", "", code.strip())
            if code:
                break
        except Exception:
            if attempt == 2:
                return None
            continue
    if not code or len(code) > 8000:
        return None
    lowered = code.lower()
    if any(b in lowered for b in _DIAGRAM_CODE_BLOCKLIST):
        return None

    import random
    import time
    import subprocess
    import sys
    name = f"diagram_gen_{int(time.time())}_{random.randint(1000, 9999)}.png"
    # Absolute path: the subprocess runs with cwd=/tmp, so a relative
    # uploads/ path would not resolve.
    out_path = os.path.abspath(os.path.join(upload_dir, name))
    # The model only builds the figure; we do the savefig so the output path
    # is always correct (models sometimes redefine OUTPUT_PATH).
    runner = (
        "import matplotlib; matplotlib.use('Agg')\n"
        f"OUTPUT_PATH = {out_path!r}\n"
        + code
        + "\nimport matplotlib.pyplot as _plt\n"
        "_plt.savefig(OUTPUT_PATH, dpi=150, bbox_inches='tight')\n"
    )
    try:
        proc = subprocess.run(
            [sys.executable, "-c", runner],
            capture_output=True,
            timeout=45,
            cwd=tempfile.gettempdir(),
        )
    except Exception:
        return None
    if proc.returncode != 0 or not os.path.exists(out_path):
        return None
    # Sanity: must be a non-trivial PNG with sane dimensions.
    try:
        if os.path.getsize(out_path) < 5000:
            os.remove(out_path)
            return None
        from PIL import Image as _PILImage
        with _PILImage.open(out_path) as _im:
            _w, _h = _im.size
        # Reject absurd aspect ratios or giant images (model bug safeguard).
        if _w < 200 or _h < 200 or _w > 3000 or _h > 3000 or max(_w, _h) / max(1, min(_w, _h)) > 4:
            os.remove(out_path)
            return None
    except Exception:
        try:
            os.remove(out_path)
        except Exception:
            pass
        return None
    # Validate the generated diagram with the vision model.
    valid, reason = _validate_diagram(out_path, question_text, reference_solution)
    if not valid:
        # Keep the image but flag it; the caller can decide to use fallback.
        # For now, return None so fallback (photo crop) can try.
        try:
            os.remove(out_path)
        except Exception:
            pass
        return None
    # Postgres mode: store diagram in DB (Render disk is ephemeral).
    try:
        import db as _db
        if _db.USE_POSTGRES:
            with open(out_path, "rb") as f:
                _db.save_image(name, f.read(), "image/png")
    except Exception:
        pass
    return {"kind": "image", "url": f"/uploads/{name}", "validated": True, "validation_note": reason}


def _crop_graph(image_paths, spec, upload_dir, question_text=""):
    """Crop a diagram region from an example photo.

    Uses a dedicated grid-overlay localization pass for accuracy; falls back
    to spec coordinates if the model provided them and localization fails.
    Returns {"kind": "image", "url": ...} or None.
    """
    from PIL import Image

    if not upload_dir:
        return None
    try:
        idx = int(spec.get("image_index", 0))
        src = image_paths[idx]
    except (TypeError, ValueError, IndexError):
        return None

    box = _localize_diagram(src, question_text)
    if box is None:
        # Fallback: coordinates from the digitization pass, if any.
        try:
            x0, y0 = float(spec.get("x_min", 0)), float(spec.get("y_min", 0))
            x1, y1 = float(spec.get("x_max", 1000)), float(spec.get("y_max", 1000))
        except (TypeError, ValueError):
            return None
        if not (0 <= x0 < x1 <= 1000 and 0 <= y0 < y1 <= 1000):
            return None
        box = (x0, y0, x1, y1)
    x0, y0, x1, y1 = box
    # Zoom out slightly: pad the box ~10% on each side so the crop isn't
    # uncomfortably tight (user feedback: previous crops cut too close).
    pad_x = (x1 - x0) * 0.10
    pad_y = (y1 - y0) * 0.10
    x0, y0 = max(0.0, x0 - pad_x), max(0.0, y0 - pad_y)
    x1, y1 = min(1000.0, x1 + pad_x), min(1000.0, y1 + pad_y)
    try:
        im = _open_image(src)
        w, h = im.size
        px = (int(w * x0 / 1000), int(h * y0 / 1000), int(w * x1 / 1000), int(h * y1 / 1000))
        if px[2] - px[0] < 20 or px[3] - px[1] < 20:
            return None
        import random
        import time
        name = f"graph_crop_{int(time.time())}_{random.randint(1000, 9999)}.png"
        im.crop(px).save(os.path.join(upload_dir, name))
        return {"kind": "image", "url": f"/uploads/{name}"}
    except Exception:
        return None


def _would_graph_help(question_text, reference_solution=""):
    """Heuristic: would a graph/diagram help understand this problem?

    Proactive visualization: even when the question has no diagram, problems
    about functions, curves, critical points, extrema, etc. benefit from one.
    """
    text = f"{question_text} {reference_solution}".lower()
    graph_keywords = (
        "plot", "graph", "sketch", "curve", "critical point", "local max",
        "local min", "extrema", "inflection", "asymptote", "intercept",
        "function f(", "f(x)", "derivative", "integral", "area under",
        "tangent line", "normal line",
    )
    return any(kw in text for kw in graph_keywords)


def _process_graph(question, image_paths, upload_dir):
    """Turn a digitized diagram into render-ready data (or None).

    Primary: generate the diagram with Python/matplotlib via the reasoning
    model (it understands the problem context and solves as needed).
    Proactive: even without has_diagram, generate one when visualization helps.
    Fallbacks: legacy function-expression sampling, then photo crop.
    """
    has_diagram = question.get("has_diagram")
    proactive = not has_diagram and _would_graph_help(
        str(question.get("question_text", "")),
        str(question.get("reference_solution", "")),
    )
    if not has_diagram and not proactive:
        return None
    # Primary: AI-generated diagram from problem context.
    gen = _generate_diagram(
        str(question.get("question_text", "")),
        str(question.get("reference_solution", "")),
        upload_dir,
        diagram_hint=str(question.get("diagram_hint", "")),
    )
    if gen:
        return gen
    # Fallback: legacy paths from older digitization specs.
    spec = question.get("graph")
    if isinstance(spec, dict):
        kind = spec.get("kind")
        if kind == "function":
            expr = str(spec.get("expression", "")).strip()
            if expr:
                sampled = _sample_function(expr, spec.get("x_min", -10), spec.get("x_max", 10))
                if sampled:
                    return sampled
        if kind == "crop":
            return _crop_graph(
                image_paths, spec, upload_dir,
                question_text=str(question.get("question_text", ""))[:300],
            )
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
    data = None
    last_err = None
    # Retry on malformed JSON (gpt-5.6-sol occasionally emits bad escapes,
    # 2026-10-08). Don't let one bad response kill the whole rubric build.
    for attempt in range(3):
        try:
            data = _chat_json(EXAMPLE_RUBRIC_SYSTEM, user_content, max_tokens=4096,
                              model="gpt-5.6-sol")
            break
        except Exception as e:  # noqa: BLE001
            last_err = e
            continue
    if data is None:
        raise RuntimeError(f"Digitization failed after 3 attempts: {last_err}")
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
                "has_diagram": bool(it.get("has_diagram", False)),
                "diagram_hint": str(it.get("diagram_hint", "")).strip(),
                "graph": it.get("graph") if isinstance(it.get("graph"), dict) else None,
            }
        )
    return questions


def validate_question(question_text, reference_solution, retry_hint="", image_paths=None):
    """Independently re-solve a question and compare with the reference solution.

    Returns {"verdict": "confirmed"|"mismatch"|"unsure", "note": str}.
    Uses an LLM re-solve plus a safe Python numeric check when applicable.
    retry_hint: previous verification failure note, to help the model
    reconsider (e.g. transcription may have misread the handwriting).
    image_paths: original photos for OCR error checking — when verification
    fails, the model can re-examine the handwriting (user req 2026-10-07).
    """
    generated_solution = None
    if not (question_text or "").strip():
        return {"verdict": "unsure", "note": "No question text to validate."}
    # If no reference solution (blank questions), generate one first, then verify.
    # (User req 2026-10-08: workflow must generate answers when missing.)
    if not (reference_solution or "").strip():
        try:
            gen = _chat_json(
                "You are an expert calculus teacher. Solve the problem completely, showing every step.",
                [{"type": "text", "text": f"Solve this problem step by step:\n\n{question_text}"}],
                max_tokens=2000,
                model="gpt-5.6-sol",
            )
            reference_solution = gen.get("solution", "") if isinstance(gen, dict) else str(gen)
            if not reference_solution.strip():
                return {"verdict": "unsure", "note": "Could not generate a solution for verification."}
            generated_solution = reference_solution
        except Exception as e:
            return {"verdict": "unsure", "note": f"Solution generation failed: {e}"}
    user_msg = (
        f"Question:\n{question_text}\n\n"
        f"Teacher's reference solution (transcribed from photo):\n{reference_solution}"
    )
    if retry_hint:
        user_msg += (
            f"\n\nPrevious verification attempt failed: {retry_hint}\n"
            "The transcription may have misread the handwriting, or the teacher "
            "may have made an error. Re-examine carefully."
        )
    # OCR error check: on retry, include the original photos so the model can
    # verify the transcription against the actual handwriting.
    # (User requirement 2026-10-07.)
    user_content = user_msg
    if retry_hint and image_paths:
        user_content = [{"type": "text", "text": user_msg}]
        for p in image_paths[:4]:  # limit to 4 images
            try:
                user_content.append({"type": "image_url", "image_url": {"url": _data_url(p)}})
            except Exception:
                pass
        user_msg += "\n\n[Original photos attached — verify the transcription against the handwriting.]"
        user_content[0] = {"type": "text", "text": user_msg}
    # Try gpt-5.6-sol (OpenAI's strongest) for verification.
    # (2026-10-07: o3 was worse than gpt-4o — more confidently wrong + crashes.)
    data = _chat_json(VALIDATE_SYSTEM, user_content,
        max_tokens=2048, model="gpt-5.6-sol",
    )
    # Fail-safe: empty/invalid LLM response (seen with o3, 2026-10-07).
    if not isinstance(data, dict):
        return {"verdict": "unsure", "note": "Verifier returned no usable response; please review manually."}
    agrees = data.get("agrees")
    confidence = str(data.get("confidence", "")).lower()
    note = str(data.get("note", "")).strip()
    claims = data.get("claims", []) or []
    py_checks = data.get("python_checks", []) or []
    # Legacy single-expression format.
    if data.get("python_check"):
        py_checks.append(str(data.get("python_check")).strip())

    # Execute all Python checks: each should evaluate to ~0 (claim verified)
    # or True. If any check proves the teacher wrong, it's a real mismatch.
    # If all pass, confirm even if the LLM's symbolic comparison disagrees.
    # (Fixes false negatives from LLM arithmetic errors, 2026-10-07.)
    check_results = []
    for expr in py_checks:
        if not expr or not isinstance(expr, str):
            continue
        computed = _safe_eval(expr)
        if computed is None:
            continue
        # Pass if ~0 (difference check) or truthy (boolean check).
        passed = abs(computed) < 1e-6 if isinstance(computed, (int, float)) else bool(computed)
        check_results.append((expr[:60], passed, computed))

    if check_results:
        failed = [r for r in check_results if not r[1]]
        if not failed:
            return {"verdict": "confirmed",
                    "note": f"Python verified {len(check_results)} claim(s). " + note}
        # Fallback: a pure expression like "6*120-2" (=718) isn't a difference
        # check — compare its value against numbers in the reference solution.
        # (Fix 2026-10-07: LLM often generates values, not zero-checks.)
        ref_nums = _digits_of(reference_solution)
        rescued = []
        still_failed = []
        for expr, passed, val in failed:
            if isinstance(val, (int, float)) and ref_nums and any(
                    abs(val - n) <= max(1e-6, abs(n) * 1e-4) for n in ref_nums):
                rescued.append((expr, val))
            else:
                still_failed.append((expr, passed, val))
        if rescued and not still_failed:
            return {"verdict": "confirmed",
                    "note": f"Python values match reference {rescued[0][1]:g}. " + note}
        if still_failed:
            # At least one check failed: report which claim is wrong.
            detail = "; ".join(f"{expr} = {val:g}" for expr, _, val in still_failed[:3])
            return {"verdict": "mismatch",
                    "note": f"Python disproves teacher claim(s): {detail}. " + note}

    # No executable checks: fall back to LLM judgment, but require high
    # confidence to overrule the teacher.
    if agrees is True:
        return {"verdict": "confirmed", "note": note, "generated_solution": generated_solution}
    if agrees is False and confidence == "high":
        return {"verdict": "mismatch",
                "note": note or "Independent check disagrees with the reference solution."}
    return {"verdict": "unsure", "note": note or "Could not confidently validate the reference solution."}



AP_RUBRIC_ONLY_SYSTEM = """You are an expert AP Calculus teacher creating a scoring rubric.
Given a question, its correct reference solution, and the total point value:

CREATE an AP-style scoring rubric where EACH point maps to ONE distinct step of the reference solution.
- Identify the solution's natural steps (for multi-part questions, group by part: Part (a) Step 1, etc.).
- Each ap_point's "for" MUST name the step it rewards, e.g. "Step 2: applies the quotient rule to get ..." or "Part (b) Step 1: sets up the integral ...". A grader checks each labeled step against its point.
- Award points for correct mathematical work at each step, not just the final answer.
- Base every criterion on an actual step visible in the reference solution.

AP conventions to follow:
- An arithmetic slip in one step does not forfeit later steps if the method stays correct (follow-through credit).
- Any mathematically valid method earns full credit, even if different from the reference steps.

CRITICAL: The point breakdown MUST sum to EXACTLY the given max_score. You are distributing exactly that many points — never more, never fewer.

Output ONLY a JSON object, no explanations:
{
  "ap_points": [{"points": 1, "for": "<step label>: <what earns this point>"}, ...]
}
"""


def _is_ap_format(criteria):
    """True if criteria is already in AP step format ('N pt: Step M: ...')."""
    c = (criteria or "").strip()
    if not c:
        return False
    # Need at least one "N pt:" segment referencing a Step.
    pts = [seg.strip() for seg in c.split(";") if seg.strip()]
    ap_count = 0
    for seg in pts:
        if re.match(r"\d+\s*pt\s*:", seg, re.IGNORECASE) and re.search(r"Step\s*\d+", seg, re.IGNORECASE):
            ap_count += 1
    # Majority of segments must be AP-formatted.
    return ap_count > 0 and ap_count >= len(pts) / 2


def _minimal_ap_rubric(reference_solution, max_score, q_label=""):
    """Structural fallback: build a minimal AP-format rubric from solution steps.

    Used when the model fails to produce AP-format rubrics after 5 attempts.
    Splits the solution into numbered steps and distributes points evenly.
    The format is always valid AP; the teacher reviews the content.
    """
    # Split solution into steps on "Step N:" markers or line breaks.
    steps = re.findall(r"Step\s*\d+\s*:\s*(.+?)(?=Step\s*\d+\s*:|$)",
                       reference_solution or "", re.IGNORECASE | re.DOTALL)
    if not steps:
        steps = [s.strip() for s in (reference_solution or "").split("\n") if s.strip()]
    steps = [s.strip()[:80] for s in steps if s.strip()][:10] or ["Complete the solution"]
    # Cap steps at total points so each step gets >= 1 pt and the sum is exact.
    # Handle fractional max_score (e.g. 2.5) by rounding to nearest int for distribution.
    total = max(1, int(round(max_score))) if max_score else len(steps)
    steps = steps[:total]
    n = len(steps)
    base, extra = divmod(total, n)
    parts = []
    for i, s in enumerate(steps):
        pts = base + (1 if i < extra else 0)
        # Truncate step description to keep criteria readable.
        desc = (s[:60] + "...") if len(s) > 60 else s
        parts.append(f"{pts} pt: Step {i + 1}: {desc}")
    return "; ".join(parts) + " [AP format - content needs teacher review]"


def generate_ap_rubric_only(question_text, reference_solution, max_score, q_label="", retry_hint="", teacher_criteria=""):
    """Generate AP-style rubric points from an existing question + solution.

    Used when the teacher's example has no usable rubric markings — instead of
    making the teacher click "Compare with AP Standard", we generate the AP
    rubric directly. Returns a criteria string; points sum to max_score.

    If teacher_criteria is provided, incorporate the teacher's scoring intent
    into the AP steps (user requirement 2026-10-07: always AP-format).
    """
    prompt = (
        f"Question {q_label} (worth {max_score:g} points):\n{question_text}\n\n"
        f"Reference solution:\n{reference_solution}\n\n"
    )
    if teacher_criteria and teacher_criteria.strip():
        prompt += (
            f"Teacher's original scoring notes (preserve their intent in your AP steps):\n"
            f"{teacher_criteria.strip()}\n\n"
        )
    prompt += "Create the AP-style scoring rubric."
    if retry_hint:
        prompt += (
            "\n\nIMPORTANT — A previous attempt failed validation. Learn from this:\n"
            f"{retry_hint}\nCorrect the rubric accordingly."
        )
    data = _chat_json(
        AP_RUBRIC_ONLY_SYSTEM,
        [{"type": "text", "text": prompt}],
        max_tokens=2048,
        model=REASONING_MODEL,
    )
    pts = data.get("ap_points") or []
    # Hard guardrails on the rubric points. The solution is the teacher's,
    # so pass it for step cross-checking but skip the substance check.
    pts, issues = _check_ap_output(None, pts, max_score, strict_steps=False)
    if issues:
        raise ValueError("Rubric validation failed: " + "; ".join(issues))
    criteria = "; ".join(
        f"{p.get('points', 1)} pt: {p.get('for', '')}"
        for p in pts if isinstance(p, dict)
    )
    return criteria


def _criteria_is_weak(criteria):
    """True if the digitized criteria has no real scoring rules."""
    c = (criteria or "").strip()
    if not c:
        return True
    if len(c) < 40:
        return True
    weak_markers = ["please confirm", "no rubric", "not shown", "unclear"]
    return any(m in c.lower() for m in weak_markers)


def build_example_rubric_streaming(image_paths, upload_dir=None, max_workers=5,
                                   on_total=None, on_item=None):
    """Full pipeline with per-question progress.

    Digitizes the example, then validates each reference solution concurrently
    (independent re-solve + Python numeric check). After each question is fully
    validated (including the one retry pass for failures), on_item(item) is
    called so the caller can stream it to the UI immediately. on_total(total)
    is called once the question count is known.

    Each item has idx (0-based original order), q, question_text,
    reference_solution, max_score, criteria, verified ("yes"|"review"),
    verify_note, and graph.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _build(idx, q, verdict):
        criteria = q["criteria"]
        # Always convert to AP-style steps (user requirement 2026-10-07),
        # preserving teacher intent from their original criteria.
        if not _is_ap_format(criteria) and q.get("reference_solution", "").strip():
            try:
                criteria = generate_ap_rubric_only(
                    q["question_text"], q["reference_solution"],
                    q["max_score"] or 5, q["q"],
                    teacher_criteria=criteria)
            except Exception:
                pass  # keep the original criteria on failure
        return {
            "idx": idx,
            "q": q["q"],
            "question_text": q["question_text"],
            "reference_solution": q["reference_solution"],
            "max_score": q["max_score"],
            "criteria": criteria,
            "verified": "yes" if verdict["verdict"] == "confirmed" else "review",
            "verify_note": "" if verdict["verdict"] == "confirmed" else verdict["note"],
            "graph": _process_graph(q, image_paths, upload_dir),
        }

    def _validate(q):
        return validate_question(q.get("question_text", ""), q.get("reference_solution", ""))

    questions = digitize_example(image_paths)
    total = len(questions)
    if on_total:
        on_total(total)

    results = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = {pool.submit(_validate, q): i for i, q in enumerate(questions)}
        verdicts = {}
        for f in as_completed(futs):
            i = futs[f]
            v = f.result()
            verdicts[i] = v
            if v["verdict"] == "confirmed":
                item = _build(i, questions[i], v)
                results[i] = item
                if on_item:
                    on_item(item)

    # Retry the failures once: re-digitize with a correction hint, then re-validate.
    failed = [i for i, v in verdicts.items() if v["verdict"] != "confirmed"]
    if failed:
        hint_bits = []
        for i in failed:
            q = questions[i]
            hint_bits.append(
                f'{q["q"]}: you transcribed the solution as "{q["reference_solution"]}", '
                f'but validation says: {verdicts[i]["note"]}'
            )
        hint = "Some transcriptions failed validation:\n" + "\n".join(hint_bits)
        questions = digitize_example(image_paths, hint=hint)
        if on_total:
            on_total(len(questions))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futs = {pool.submit(_validate, questions[i]): i for i in failed if i < len(questions)}
            for f in as_completed(futs):
                i = futs[f]
                v = f.result()
                item = _build(i, questions[i], v)
                results[i] = item
                if on_item:
                    on_item(item)

    return {"items": [results[i] for i in sorted(results)]}


def build_example_rubric(image_paths, upload_dir=None, max_workers=5):
    """Full pipeline: digitize the example, validate each reference solution
    (independent re-solve + Python numeric check), retry failed questions once
    with a correction hint, and flag anything still unresolved for the teacher.

    Returns {"items": [...]} where each item has idx, q, question_text,
    reference_solution, max_score, criteria, verified ("yes"|"review"),
    verify_note, and graph (render-ready function plot / cropped image / None).
    """
    return build_example_rubric_streaming(image_paths, upload_dir=upload_dir,
                                         max_workers=max_workers)


def rubric_from_example(image_paths, upload_dir=None):
    """Legacy alias: build the full digitize+validate rubric from example photos."""
    return build_example_rubric(image_paths, upload_dir=upload_dir)


# ---------------------------------------------------------------------------
# Solve-first flow: the teacher uploads question photos with NO answer key
# (or untrusted student work). The AI solves each question itself, verifies
# the solution, and generates an AP-style rubric.
# ---------------------------------------------------------------------------

QUESTIONS_ONLY_SYSTEM = """You are digitizing quiz/exam questions from photos into structured data.
For each question, extract:
- "q": the question label, use short format like "Q1", "Q2", "Q3".
- "question_text": the PRINTED/TYPED question text, transcribed faithfully. Write ALL mathematics in LaTeX: use $...$ for inline math and $$...$$ for display math. EVERY math fragment MUST be wrapped in $ delimiters.
- "max_score": the point value printed for the question (e.g. "[4 pts]" -> 4). If no point value is printed, use 0.
- "has_diagram": true or false — REQUIRED. Does this question show ANY diagram, graph, plot, coordinate system, or geometric figure?
- "diagram_hint": REQUIRED when has_diagram is true, omitted otherwise. One sentence describing what the diagram shows.

CRITICAL: IGNORE ALL HANDWRITING. The photos may contain handwritten student work or teacher markings — do NOT transcribe them, do NOT treat them as the answer. Transcribe ONLY the printed/typed question.

Output ONLY a JSON object, no explanations: {"questions": [{"q": "Q1", "question_text": "...", "max_score": 4, "has_diagram": false}, ...]}
If the photos yield no questions at all, output {"questions": []}.
"""


AP_PLAN_SYSTEM = """You are an expert AP Calculus teacher designing the scoring plan for a question BEFORE anyone solves it.
Given a question and its point value:

1. Break the question into its natural AP-style STEPS (Step 1, Step 2, ...). Each step is one distinct, checkable mathematical action an AP grader would award points for.
   - For multi-part questions, group by part: Part (a) Step 1, Part (a) Step 2, Part (b) Step 1, etc.
   - Typical steps: SETUP (writing the rule/formula to use), MATHEMATICAL WORK (differentiating, simplifying, substituting values), final ANSWER (with units/justification where applicable).
2. Assign points to each step following AP free-response conventions:
   - Points reward correct mathematical work at each step, not just the final answer.
   - An arithmetic slip in one step does not forfeit later steps if the method stays correct (follow-through credit).
   - Any mathematically valid method earns full credit.

CRITICAL: The point breakdown MUST sum to EXACTLY the given max_score. You are distributing exactly that many points — never more, never fewer.

Output ONLY a JSON object, no explanations:
{
  "ap_points": [{"points": 1, "for": "Step 1: <what this step requires>"}, ...]
}
"""


SOLVE_FROM_PLAN_SYSTEM = """You are an expert calculus teacher writing a complete worked solution.
You are GIVEN the question and its AP scoring plan (the numbered steps and what each requires).

Write the full solution FOLLOWING THE PLAN'S STEPS EXACTLY:
- Use the same step labels in the same order (Step 1, Step 2, ...).
- Each step must show the actual mathematical work — no skipped algebra.
- The solution will be used as the answer key for grading, so every step must be correct and verifiable.

Output ONLY a JSON object, no explanations:
{
  "solution": "<numbered steps, e.g. 'Step 1: ...\\nStep 2: ...'. Mathematics in LaTeX with $...$ delimiters>"
}
"""


SOLVE_RUBRIC_SYSTEM = """You are an expert AP Calculus teacher creating an answer key and scoring rubric from scratch.
Given a question and its point value:

1. SOLVE the question completely and correctly, writing the solution as NUMBERED steps (Step 1, Step 2, ...). Break the solution into its natural mathematical steps — each step is one distinct, checkable action:
   - For multi-part questions, group steps by part: Part (a) Step 1, Part (a) Step 2, Part (b) Step 1, etc.
   - Typical steps: setup (writing the rule/formula to use), computation (differentiating, simplifying, substituting), and final answer (with units/justification where applicable).
   - Show every step — no skipped algebra. This answer key will be used to grade students, so each step must be verifiable.

2. CREATE an AP-style scoring rubric where EACH point maps to ONE solution step. The "for" of each ap_point MUST start with the step label, e.g. "Step 2: correctly applies the quotient rule to get ...". A grader checks each labeled step against its point — no guessing which step earns what.

DIAGRAM REQUIREMENT: If the question mentions "graph", "sketch", "plot", or "diagram" (e.g. "Graph the function", "sketch the graph of f(x)"), the solution MUST describe the required diagram in detail (axes, key points, asymptotes, curve shape, labels). The system generates diagrams with Python/matplotlib from your description, so be specific about coordinates and visual features.

AP conventions to follow:
- Points reward correct mathematical work at each step, not just the final answer.
- An arithmetic slip in one step does not forfeit later steps if the method stays correct (follow-through credit).
- Any mathematically valid method earns full credit, even if different from your steps.

CRITICAL: The point breakdown MUST sum to EXACTLY the given max_score. You are distributing exactly that many points — never more, never fewer.

Output ONLY a JSON object, no explanations:
{
  "solution": "<numbered steps, e.g. 'Step 1: ...\\nStep 2: ...'. Mathematics in LaTeX with $...$ delimiters>",
  "ap_points": [{"points": 1, "for": "Step 1: <what earns this point>"}, ...],
  "notes": "<any ambiguity in the question, or empty>"
}
"""


VERIFY_SOLUTION_SYSTEM = """You are a careful checker. You are given a calculus question and a teacher's worked solution.
Your job is NOT to re-solve from scratch (that causes false mismatches). Instead:
1. Read the teacher's solution carefully. Extract the KEY mathematical claims (e.g. "critical points are x=-1/2, 1/2, 3", "f'(x) = 4x^3 - 12x^2 - x + 3").
2. For each key claim, provide a Python expression (using only arithmetic operators and the math module) that VERIFIES the claim by direct computation. Example: to verify "f'(x) = 4x^3 - 12x^2 - x + 3" at x=2, use "(4*2**3 - 12*2**2 - 2 + 3) - (4*2**3 - 12*2**2 - 2 + 3)" which should be 0. To verify critical points, plug each into f'(x) and check it equals 0.
3. EQUIVALENT FORMS: If the teacher's answer looks different from what you'd expect (e.g. "y - √2/2 = √2(x-π/4)" vs "y = √2(x-π/4) + √2/2"), do NOT flag it as wrong. Instead, verify by SUBSTITUTION: pick a specific x value, compute y from both forms, and check they match. Only flag if substitution PROVES they're different.
4. If the teacher's solution is correct, the Python checks will pass. Only flag a problem if the Python computation PROVES the teacher wrong.
5. DIAGRAM CHECK: If the question mentions "graph", "sketch", "plot", or "diagram", verify that the solution describes or includes the required diagram. If the solution lacks a diagram description for a graph-type question, note this in the "note" field.
Output ONLY a JSON object, no explanations:
{"claims": ["<claim 1>", "<claim 2>"], "python_checks": ["<expr 1>", "<expr 2>"], "agrees": true/false, "confidence": "high/medium/low", "note": "<one sentence, or empty>"}
Set "agrees" to true if the teacher's claims verify (or if there are no verifiable numerical claims but the reasoning looks sound).
"""


def transcribe_questions_only(image_paths):
    """Vision pass: read ONLY the printed questions and point values.

    Handwriting in the photos is ignored. Returns a list of
    {q, question_text, max_score, has_diagram, diagram_hint}.
    """
    if not image_paths:
        raise RuntimeError("No question photos received. Please take or upload the questions first.")
    user_content = [
        {
            "type": "text",
            "text": "These photos show quiz/exam questions. Transcribe ONLY the printed questions and their point values. Ignore all handwriting.",
        }
    ]
    for p in image_paths:
        user_content.append({"type": "image_url", "image_url": {"url": _data_url(p)}})
    data = _chat_json(QUESTIONS_ONLY_SYSTEM, user_content, max_tokens=4096,
                      model="gpt-5.6-sol")
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
                "max_score": ms,
                "has_diagram": bool(it.get("has_diagram", False)),
                "diagram_hint": str(it.get("diagram_hint", "")).strip(),
            }
        )
    return questions


def solve_and_rubric(question_text, max_score, q_label="", image_paths=None,
                   model=None, reasoning_effort=None, _debug=None, retry_hint=""):
    """AP-first: plan the steps + rubric from the question, then solve following the plan.

    Phase 1: Generate the AP step plan (numbered steps + point breakdown) from
    the question alone — the rubric drives the solution, not the other way around.
    Phase 2: Write the complete worked solution following the plan's steps exactly.

    Returns {solution, ap_points, notes}. The ap_points sum to exactly max_score.
    When image_paths is given, the model also sees the original photos so it
    can read the PRINTED question directly (guarding against transcription
    errors); it must still ignore all handwriting.
    `retry_hint` (optional) describes what went wrong in a previous attempt so
    the model can correct itself.
    """
    use_model = model or REASONING_MODEL

    # Shared image content (both phases see the printed question).
    def _with_images(text):
        user_content = [{"type": "text", "text": text}]
        if image_paths:
            user_content[0]["text"] += (
                "\n\nThe attached photos show the printed question. Read the PRINTED "
                "question text from the photos to double-check the transcription above "
                "(in particular, read exponents and fractions very carefully). "
                "IGNORE all handwriting in the photos."
            )
            for p in image_paths:
                try:
                    user_content.append({"type": "image_url", "image_url": {"url": _data_url(p)}})
                except Exception:
                    pass
        return user_content

    # ---- Phase 1: AP plan (steps + rubric) from the question ----
    plan_prompt = (
        f"Question {q_label} (worth {max_score:g} points):\n{question_text}\n\n"
        "Design the AP scoring plan: break into steps and assign points."
    )
    if retry_hint:
        plan_prompt += (
            "\n\nIMPORTANT — A previous attempt failed. Learn from this:\n"
            f"{retry_hint}\n"
            "Fix the plan accordingly (clearer steps, correct point distribution)."
        )
    plan_debug = []
    plan_data = _chat_json(
        AP_PLAN_SYSTEM,
        _with_images(plan_prompt),
        max_tokens=2048,
        model=use_model,
        reasoning_effort=reasoning_effort,
        _raw=plan_debug,
    )
    if _debug is not None:
        for d in plan_debug:
            d = dict(d)
            d["phase"] = "ap_plan"
            _debug.append(d)
    pts = plan_data.get("ap_points") or []
    # Validate the plan on its own (no solution yet): steps sequential, sum correct.
    pts, plan_issues = _check_ap_output(None, pts, max_score, strict_steps=False)
    if plan_issues:
        raise ValueError("AP plan validation failed: " + "; ".join(plan_issues))

    # ---- Phase 2: Solution following the plan's steps exactly ----
    plan_text = "\n".join(
        f'{p.get("points", 1)} pt — {p.get("for", "")}' for p in pts
    )
    solve_prompt = (
        f"Question {q_label} (worth {max_score:g} points):\n{question_text}\n\n"
        f"AP scoring plan (follow these steps EXACTLY):\n{plan_text}\n\n"
        "Write the complete worked solution using the plan's step labels in order."
    )
    if retry_hint:
        solve_prompt += (
            "\n\nIMPORTANT — A previous attempt failed. Learn from this:\n"
            f"{retry_hint}\n"
            "Make sure the solution follows every plan step with correct mathematics."
        )
    solve_debug = []
    solve_data = _chat_json(
        SOLVE_FROM_PLAN_SYSTEM,
        _with_images(solve_prompt),
        max_tokens=4096,
        model=use_model,
        reasoning_effort=reasoning_effort,
        _raw=solve_debug,
    )
    if _debug is not None:
        for d in solve_debug:
            d = dict(d)
            d["phase"] = "solution"
            _debug.append(d)
    solution = normalize_latex(str(solve_data.get("solution", "")).strip())
    # Validate the solution follows the plan: substantive, and every planned
    # step label appears in the solution text.
    sol_issues = []
    if len(solution.strip()) < 50:
        sol_issues.append(
            f"Solution is empty or too short ({len(solution.strip())} chars). "
            "Write the complete worked solution following every plan step.")
    # Every step referenced by the rubric must appear in the solution.
    for p in pts:
        m = re.search(r"(?:Part\s*\(([a-zA-Z])\)\s*)?Step\s*(\d+)",
                      str(p.get("for", "")), re.IGNORECASE)
        if not m:
            continue
        part = m.group(1).lower() if m.group(1) else None
        num = int(m.group(2))
        # Look for this specific step label in the solution.
        # \b prevents "Step 1" from matching "Step 10".
        pat = r"Step\s*%d\b" % num
        if part:
            pat = r"Part\s*\(%s\)\s*Step\s*%d\b" % (part, num)
        if not re.search(pat, solution, re.IGNORECASE):
            label = f"Step {num}" + (f" of Part ({part})" if part else "")
            sol_issues.append(
                f"Solution is missing {label} from the AP plan. "
                "Follow every plan step in order.")
    if sol_issues:
        raise ValueError("Solution validation failed: " + "; ".join(sol_issues))

    return {
        "solution": solution,
        "ap_points": pts,
        "notes": "",
    }


def verify_solution(question_text, solution, model=None, reasoning_effort=None, _debug=None):
    """Independently re-solve and compare with the generated answer key.

    Returns {"verdict": "confirmed"|"mismatch"|"unsure", "note": str}.
    """
    if not (question_text or "").strip() or not (solution or "").strip():
        return {"verdict": "unsure", "note": "No question or solution to verify."}
    data = _chat_json(
        VERIFY_SOLUTION_SYSTEM,
        [{"type": "text", "text": f"Question:\n{question_text}\n\nProposed solution (AI-generated answer key):\n{solution}"}],
        max_tokens=2048,
        model=model or REASONING_MODEL,
        reasoning_effort=reasoning_effort,
        _raw=_debug,
    )
    agrees = data.get("agrees")
    confidence = str(data.get("confidence", "")).lower()
    note = str(data.get("note", "")).strip()
    your_answer = str(data.get("your_answer", "")).strip()

    py_expr = str(data.get("python_check", "")).strip()
    if py_expr:
        computed = _safe_eval(py_expr)
        ref_nums = _digits_of(solution)
        if computed is not None and ref_nums:
            if any(abs(computed - n) <= max(1e-6, abs(n) * 1e-4) for n in ref_nums):
                return {"verdict": "confirmed", "note": f"Python check passed ({py_expr} = {computed:g})."}
            return {
                "verdict": "mismatch",
                "note": f"Python check: {py_expr} = {computed:g}, but the answer key says {solution!r}.",
            }

    if agrees is True and confidence in ("high", "medium"):
        detail = f" Independent solution: {your_answer}." if your_answer else ""
        return {"verdict": "confirmed", "note": (note + detail).strip()}
    if agrees is False:
        detail = f" Independent solution: {your_answer}." if your_answer else ""
        return {"verdict": "mismatch", "note": (note + detail).strip() or "Independent solving disagrees with the answer key."}
    return {"verdict": "unsure", "note": note or "Could not confidently verify the answer key."}


def retry_solve_question(question_text, max_score, q_label="", model="o3",
                         high_effort=False, image_paths=None, max_attempts=3):
    """Retry solving a single question, with full debug output.

    `model` is "o3" or "gpt-4o". `high_effort` requests o3 with
    reasoning_effort="high" (slower, more thorough). Tries up to
    `max_attempts` times; each retry sees the previous failure.

    Returns {solution, criteria, verified, verify_note, debug} where debug is
    a list of {step, model, raw} showing exactly what each model call returned.
    """
    debug = []
    effort = "high" if high_effort and model.startswith("o") else None
    use_model = model or REASONING_MODEL

    solved, verdict, last_exc = None, None, None
    retry_hint = ""
    prev_solution = None
    for attempt in range(1, max_attempts + 1):
        solve_debug, verify_debug = [], []
        try:
            solved = solve_and_rubric(
                question_text, max_score, q_label,
                image_paths=image_paths, model=use_model,
                reasoning_effort=effort, _debug=solve_debug,
                retry_hint=retry_hint)
            for d in solve_debug:
                phase = d.get("phase", "solve")
                debug.append({"step": f"{phase} (try {attempt})", "model": d["model"],
                              "effort": d["effort"], "raw": d["raw"]})
            verdict = verify_solution(question_text, solved["solution"],
                                      model=use_model, reasoning_effort=effort,
                                      _debug=verify_debug)
            for d in verify_debug:
                debug.append({"step": f"verify (try {attempt})", "model": d["model"],
                              "effort": d["effort"], "raw": d["raw"]})
        except Exception as e:  # noqa: BLE001
            last_exc = e
            debug.append({"step": f"solve (try {attempt})", "model": use_model,
                          "effort": effort, "raw": f"ERROR: {e}"})
            retry_hint = f"Attempt {attempt} raised an error: {e}. Try again carefully."
            continue
        if verdict["verdict"] == "confirmed":
            break
        # Same answer twice: further retries won't help.
        cur = (solved["solution"] or "").strip()
        if prev_solution is not None and cur == prev_solution:
            verdict = {"verdict": "review",
                       "note": (verdict.get("note", "") +
                                " (Same answer twice; further retries unlikely to help.)").strip()}
            break
        prev_solution = cur
        retry_hint = (
            f"Attempt {attempt} was NOT confirmed ({verdict['verdict']}): "
            f"{verdict.get('note', '')} Attempted solution: {solved['solution'][:500]}"
        )

    if solved is None:
        raise last_exc or RuntimeError("All retry attempts failed.")
    verified = "yes" if verdict["verdict"] == "confirmed" else "review"
    criteria = "; ".join(
        f"{p.get('points', 1)} pt: {p.get('for', '')}"
        for p in solved["ap_points"] if isinstance(p, dict)
    )
    note = verdict.get("note", "")
    if verified == "review" and not note:
        note = f"Still not verified after {max_attempts} attempts. Please review manually."
    return {
        "solution": solved["solution"],
        "criteria": criteria,
        "max_score": max_score,
        "verified": verified,
        "verify_note": note,
        "debug": debug,
    }



def _check_ap_output(solution, ap_points, max_score, strict_steps=True):
    """Hard guardrails on AI-generated solution + AP rubric.

    Returns (fixed_points, issues). `fixed_points` has a ±1 point-sum diff
    auto-corrected on the last point. `issues` is a list of problem strings;
    empty means the output is OK. Callers should retry with the issues as
    feedback, or flag for manual review if retries are exhausted.

    Checks:
    1. Solution is non-empty and substantive (>= 50 chars). Skipped when
       solution is None (teacher-provided).
    2. Every rubric point references a step label. When strict_steps=True,
       the label must exist in the solution text (e.g. "Step 2:" requires
       "Step 2" in the solution). When False (teacher solution without
       numbered steps), step numbers must simply be sequential from 1.
    3. Granularity: a question worth >= 4 points needs at least 2 rubric points.
    4. Point sum: auto-fix ±1 on the last point; larger diffs are issues.
    """
    issues = []
    pts = [p for p in (ap_points or []) if isinstance(p, dict)]

    # 1. Solution substance (skip when solution is teacher-provided / None).
    if solution is not None:
        sol = (solution or "").strip()
        if len(sol) < 50:
            issues.append(
                f"Solution is empty or too short ({len(sol)} chars). "
                "Write the complete worked solution with numbered steps.")

    # 2. Collect step labels present in the solution.
    steps_found = set()
    if solution:
        for m in re.finditer(r"(?:Part\s*\(([a-zA-Z])\)\s*)?Step\s*(\d+)",
                             solution, re.IGNORECASE):
            part = m.group(1).lower() if m.group(1) else None
            steps_found.add((part, int(m.group(2))))

    # 3. Rubric points exist and are granular enough.
    target = int(round(max_score))
    if not pts:
        issues.append("No rubric points generated. Break the solution into "
                      "steps and assign points to each.")
    else:
        if target >= 4 and len(pts) < 2:
            issues.append(
                f"Only {len(pts)} rubric point(s) for a {target}-point question. "
                "Break the solution into at least 2 steps with separate points.")
        # Every point must reference a step. Collect referenced step numbers.
        ref_steps = []
        for i, p in enumerate(pts):
            for_text = str(p.get("for", ""))
            m = re.search(r"(?:Part\s*\(([a-zA-Z])\)\s*)?Step\s*(\d+)",
                          for_text, re.IGNORECASE)
            if not m:
                issues.append(
                    f'Rubric point {i + 1} does not reference a solution step '
                    '(its "for" must start with the step label, e.g. "Step 2: ...").')
                continue
            part = m.group(1).lower() if m.group(1) else None
            key = (part, int(m.group(2)))
            ref_steps.append(key)
            if strict_steps and solution is not None and key not in steps_found:
                label = f"Step {key[1]}" + (f" of Part ({key[0]})" if key[0] else "")
                issues.append(
                    f"Rubric point {i + 1} references {label}, which does "
                    "not exist in the solution steps.")
        # Non-strict mode: step numbers must be sequential from 1.
        if not strict_steps and ref_steps:
            nums = sorted(n for _, n in ref_steps)
            if nums != list(range(1, len(nums) + 1)):
                issues.append(
                    f"Rubric step numbers are not sequential from 1: {nums}. "
                    "Number the steps 1, 2, 3, ... in order.")

    # 4. Point sum: auto-fix ±1, larger diffs are issues.
    total = sum(int(p.get("points", 0) or 0) for p in pts)
    diff = target - total
    if diff != 0:
        if abs(diff) == 1 and pts:
            pts[-1]["points"] = int(pts[-1].get("points", 0) or 0) + diff
        else:
            issues.append(
                f"Rubric points sum to {total}, not {target}. "
                f"Redistribute to sum to exactly {target}.")

    return pts, issues


def _solve_with_retries(question_text, max_score, q_label="", image_paths=None,
                       max_attempts=3):
    """Solve + verify with up to `max_attempts` tries.

    Each retry includes the previous attempt's error/verification note in the
    prompt so the model can correct itself. Returns (solved, verdict) on
    success; raises the last exception if all attempts fail with an error.
    If verification keeps failing (not an exception), returns the last
    (solved, verdict) pair so the caller can flag it for manual review.
    """
    last_exc = None
    solved, verdict = None, None
    retry_hint = ""
    prev_solution = None
    for attempt in range(1, max_attempts + 1):
        try:
            solved = solve_and_rubric(question_text, max_score, q_label,
                                      image_paths=image_paths,
                                      retry_hint=retry_hint)
            verdict = verify_solution(question_text, solved["solution"])
        except Exception as e:  # noqa: BLE001
            last_exc = e
            retry_hint = (
                f"Attempt {attempt} raised an error: {e}. "
                "This may have been transient — try again carefully."
            )
            continue
        if verdict["verdict"] == "confirmed":
            return solved, verdict
        # If the model produced the exact same solution twice, retrying is
        # pointless — stop early and let the teacher review.
        cur = (solved["solution"] or "").strip()
        if prev_solution is not None and cur == prev_solution:
            verdict = {"verdict": "review",
                       "note": (verdict.get("note", "") +
                                " (Model produced the same answer twice; further retries unlikely to help.)").strip()}
            break
        prev_solution = cur
        # Verification failed: feed the discrepancy back for the next attempt.
        retry_hint = (
            f"Attempt {attempt} produced a solution, but independent verification "
            f"did NOT confirm it ({verdict['verdict']}): {verdict.get('note', '')} "
            f"The attempted solution was: {solved['solution'][:500]}"
        )
    # All attempts exhausted.
    if last_exc is not None and solved is None:
        raise last_exc
    return solved, verdict



def build_unified_rubric_streaming(image_paths, upload_dir=None, max_workers=3,
                                   on_total=None, on_item=None):
    """One-button rubric pipeline: example-aware, AP-style rubrics.

    1. Digitizes the photos (printed questions + point values; teacher's
       worked solutions/answer key IF shown — blank questions get an empty
       reference_solution).
    2. Per question, concurrently:
       - If a teacher reference solution was found: validate it and use it.
         If the teacher's markings yield no usable scoring rules, generate
         an AP-style rubric from the reference solution automatically.
       - If no reference solution: solve with the reasoning model (up to 3
         attempts, each seeing the previous failure) and generate an AP-style
         rubric from the AI solution.
    3. Per-question max_score always comes from the printed point value
       (teacher-assigned); AP points are distributed to sum to exactly that.
    4. Streams each completed item via on_item(item).

    Returns {"items": [...]} with idx, q, question_text, reference_solution,
    max_score, criteria, verified ("yes"|"review"), verify_note, origin, graph.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _build_from_example(idx, q):
        """Example path: teacher's solution exists."""
        q_label = q.get("q", f"Q{idx + 1}")
        max_score = q.get("max_score", 0) or 0
        if not max_score:
            max_score = 5
            default_note = "Max score set by AI default — please confirm."
        else:
            default_note = ""
        # Retry validation up to 3 times, feeding each failure's note back as
        # context (the transcription may have misread handwriting, or the
        # teacher may be wrong). Only after 3 tries do we ask the user.
        # (User requirement 2026-10-07: always retry with error info.)
        verdict = None
        last_note = ""
        for _ in range(3):
            try:
                verdict = validate_question(q.get("question_text", ""),
                                            q.get("reference_solution", ""),
                                            retry_hint=last_note,
                                            image_paths=image_paths)
                # If a solution was generated during validation, save it back
                if verdict.get("generated_solution"):
                    q["reference_solution"] = verdict["generated_solution"]
            except Exception as e:  # noqa: BLE001
                last_note = str(e)
                continue
            if verdict["verdict"] == "confirmed":
                break
            last_note = verdict.get("note", "")
        if verdict is None or verdict["verdict"] != "confirmed":
            verdict = {"verdict": "review",
                       "note": f"Not confirmed after 3 attempts ({last_note}). "
                               "Please review manually."}
        criteria = q.get("criteria", "")
        # AP enforcement: ALWAYS convert to AP-style steps, even if the teacher
        # wrote criteria. The teacher's wording is passed as context so the
        # AP rubric preserves their intent. (User requirement 2026-10-07.)
        if not _is_ap_format(criteria):
            # AP enforcement: the rubric MUST be decomposed into AP-style steps.
            # Retry with increasingly forceful reprompts; never keep weak
            # "deduct..." text. If the model still fails, build a minimal
            # AP-format rubric from the solution steps so the format is right.
            rubric_hint = ""
            ap_ok = False
            for attempt in range(5):
                try:
                    if attempt >= 3:
                        # Final attempts: show a concrete example of the required format.
                        rubric_hint += (
                            "\n\nFORMAT EXAMPLE (you MUST follow this exact structure):\n"
                            "\"1 pt: Step 1: ...; 1 pt: Step 2: ...; 2 pt: Step 3: ...\"\n"
                            "Each segment MUST start with '<N> pt:' and reference 'Step <M>'. "
                            "Decompose the solution into distinct checkable steps. "
                            "Do NOT write general deductions like 'deduct for incorrect...'."
                        )
                    criteria = generate_ap_rubric_only(
                        q.get("question_text", ""), q.get("reference_solution", ""),
                        max_score, q_label, retry_hint=rubric_hint,
                        teacher_criteria=q.get("criteria", ""))
                    if _is_ap_format(criteria):
                        ap_ok = True
                        break
                    rubric_hint = (
                        f"Your output was not in AP step format: {criteria[:200]}. "
                        "Rewrite it as '<N> pt: Step <M>: <what to check>; ...'."
                    )
                except Exception as e:  # noqa: BLE001
                    rubric_hint = str(e)
            if not ap_ok:
                # Structural fallback: minimal AP-format rubric from solution steps.
                # Better than weak "deduct..." text; teacher reviews the content.
                criteria = _minimal_ap_rubric(
                    q.get("reference_solution", ""), max_score, q_label)
        if default_note and default_note not in (criteria or ""):
            criteria = f"{criteria} ({default_note})" if criteria else default_note
        return {
            "idx": idx,
            "q": q_label,
            "question_text": q.get("question_text", ""),
            "reference_solution": q.get("reference_solution", ""),
            "max_score": max_score,
            "criteria": criteria,
            "has_diagram": q.get("has_diagram", False),
            "verified": "yes" if verdict["verdict"] == "confirmed" else "review",
            "verify_note": "" if verdict["verdict"] == "confirmed" else verdict["note"],
            "origin": "teacher_example",
            "graph": _process_graph(q, image_paths, upload_dir),
        }

    def _build_from_solve(idx, q):
        """Solve path: no teacher solution — AI solves it."""
        q_label = q.get("q", f"Q{idx + 1}")
        max_score = q.get("max_score", 0) or 0
        if not max_score:
            max_score = 5
            default_note = "Max score set by AI default — please confirm."
        else:
            default_note = ""
        try:
            solved, verdict = _solve_with_retries(
                q.get("question_text", ""), max_score, q_label,
                image_paths=image_paths, max_attempts=3)
        except Exception as e:  # noqa: BLE001
            return {
                "idx": idx,
                "q": q_label,
                "question_text": q.get("question_text", ""),
                "reference_solution": "",
                "max_score": max_score,
                "criteria": "",
                "has_diagram": q.get("has_diagram", False),
                "verified": "review",
                "verify_note": f"AI solving failed after 3 attempts ({e}). Please solve manually or tap Retry.",
                "origin": "ai_solved",
                "graph": None,
            }
        verified = "yes" if verdict["verdict"] == "confirmed" else "review"
        note = verdict.get("note", "")
        if verified == "review" and not note:
            note = "AI could not verify the solution after 3 attempts. Please review or tap Retry."
        criteria = "; ".join(
            f"{p.get('points', 1)} pt: {p.get('for', '')}"
            for p in solved["ap_points"] if isinstance(p, dict)
        )
        if default_note:
            criteria = f"{criteria} ({default_note})" if criteria else default_note
        item = {
            "idx": idx,
            "q": q_label,
            "question_text": q.get("question_text", ""),
            "reference_solution": solved["solution"],
            "max_score": max_score,
            "criteria": criteria,
            "has_diagram": q.get("has_diagram", False),
            "verified": verified,
            "verify_note": note,
            "origin": "ai_solved",
            "graph": None,
        }
        try:
            gq = dict(q)
            gq["reference_solution"] = solved["solution"]
            gq["criteria"] = criteria
            item["graph"] = _process_graph(gq, image_paths, upload_dir)
        except Exception:
            item["graph"] = None
        return item

    def _build(idx, q):
        # Route per question: teacher example if a reference solution was
        # found, otherwise AI solves it.
        if (q.get("reference_solution") or "").strip():
            return _build_from_example(idx, q)
        return _build_from_solve(idx, q)

    questions = digitize_example(image_paths)
    total = len(questions)
    if on_total:
        on_total(total)
    if not questions:
        return {"items": []}

    results = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = {pool.submit(_build, i, q): i for i, q in enumerate(questions)}
        for f in as_completed(futs):
            i = futs[f]
            try:
                item = f.result()
            except Exception as e:  # noqa: BLE001
                q = questions[i]
                item = {
                    "idx": i,
                    "q": q.get("q", f"Q{i + 1}"),
                    "question_text": q.get("question_text", ""),
                    "reference_solution": q.get("reference_solution", ""),
                    "max_score": q.get("max_score", 0) or 5,
                    "criteria": "",
                    "verified": "review",
                    "verify_note": f"Processing failed ({e}). Please review or tap Retry.",
                    "origin": "ai_solved",
                    "graph": None,
                }
            results[i] = item
            if on_item:
                on_item(item)
    return {"items": [results[i] for i in sorted(results)]}


def build_solve_rubric_streaming(image_paths, upload_dir=None, max_workers=3,
                                on_total=None, on_item=None):
    """Solve-first pipeline with per-question progress.

    1. Transcribe printed questions + point values (handwriting ignored).
    2. For each question (concurrently): solve with the reasoning model,
       verify the solution with an independent re-solve, generate an
       AP-style rubric, and generate a diagram when needed.
    3. Stream each completed item via on_item(item).

    Returns {"items": [...]} where each item has idx, q, question_text,
    reference_solution, max_score, criteria, verified ("yes"|"review"),
    verify_note, and graph.
    """
    questions = transcribe_questions_only(image_paths)
    total = len(questions)
    if on_total:
        on_total(total)
    if not questions:
        return {"items": []}

    from concurrent.futures import ThreadPoolExecutor

    def _build(idx, q):
        q_label = q.get("q", f"Q{idx + 1}")
        max_score = q.get("max_score", 0) or 0
        # If no point value was printed, let the teacher confirm later.
        if not max_score:
            max_score = 5
            default_note = "Max score set by AI default — please confirm."
        else:
            default_note = ""
        try:
            # Up to 3 attempts; each retry sees the previous failure.
            solved, verdict = _solve_with_retries(
                q.get("question_text", ""), max_score, q_label,
                image_paths=image_paths, max_attempts=3)
        except Exception as e:  # noqa: BLE001
            # Never silently drop a question: surface the error for teacher review.
            return {
                "idx": idx,
                "q": q_label,
                "question_text": q.get("question_text", ""),
                "reference_solution": "",
                "max_score": max_score,
                "criteria": "",
                "has_diagram": q.get("has_diagram", False),
                "verified": "review",
                "verify_note": f"AI solving failed after 3 attempts ({e}). Please solve manually or tap Retry.",
                "origin": "ai_solved",
                "graph": None,
            }
        verified = "yes" if verdict["verdict"] == "confirmed" else "review"
        if verified == "review" and not verdict.get("note"):
            note = "AI could not verify the solution after 3 attempts. Please review or tap Retry."
        else:
            note = verdict.get("note", "")
        criteria = "; ".join(
            f"{p.get('points', 1)} pt: {p.get('for', '')}"
            for p in solved["ap_points"] if isinstance(p, dict)
        )
        if default_note:
            criteria = f"{criteria} ({default_note})" if criteria else default_note
        item = {
            "idx": idx,
            "q": q_label,
            "question_text": q.get("question_text", ""),
            "reference_solution": solved["solution"],
            "max_score": max_score,
            "criteria": criteria,
            "has_diagram": q.get("has_diagram", False),
            "verified": verified,
            "verify_note": note,
            "origin": "ai_solved",
            "graph": None,
        }
        # Diagram for graph questions (best effort; never blocks the item).
        try:
            gq = dict(q)
            gq["reference_solution"] = solved["solution"]
            gq["criteria"] = criteria
            item["graph"] = _process_graph(gq, image_paths, upload_dir)
        except Exception:
            item["graph"] = None
        return item

    items = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_build, idx, q) for idx, q in enumerate(questions)]
        for f in futures:
            try:
                item = f.result()
            except Exception as e:  # noqa: BLE001
                item = None
            if item and on_item:
                on_item(item)
            if item:
                items.append(item)
    items.sort(key=lambda it: it.get("idx", 0))
    return {"items": items}


AP_RUBRIC_SYSTEM = """You are an AP Calculus scoring expert. Given a teacher's rubric for calculus questions, you generate an AP-style scoring rubric for each question and compare it with the teacher's rubric.

AP Calculus free-response scoring conventions:
- Points are awarded for specific steps (setup, correct mathematical work, final answer), not just the final answer.
- Typical breakdown: 1 point for correct setup (e.g., correct integral with limits), 1-2 points for correct work/computation, 1 point for the final answer (often requiring correct units or justification).
- Justification and communication matter: AP awards points for explaining reasoning, not just calculations.
- Partial credit is specific: each point has clear criteria for what earns it.

CRITICAL: The AP point breakdown must sum to EXACTLY the teacher's original max_score for that question. You are only re-distributing the teacher's points using AP-style criteria — never change the total. So ap_total must always equal the teacher's max_score shown in the input.

Output ONLY a JSON object in this format:
{
  "questions": [
    {
      "q": "Q1",
      "ap_points": [
        {"points": 1, "for": "Correct setup, e.g., ..."},
        {"points": 1, "for": "Correct computation, e.g., ..."},
        {"points": 1, "for": "Final answer with ..."}
      ],
      "ap_total": 3,
      "teacher_total": 4,
      "comparison": "Brief comparison of teacher's rubric vs AP standard.",
      "recommendation": "Specific suggestion, e.g., 'Consider adding 1 point for justification, as AP requires it.' or 'Your rubric is well-aligned with AP.'"
    }
  ],
  "overall": "Overall assessment of alignment with AP standards."
}

Be specific and actionable in recommendations. If the teacher's rubric is already AP-aligned, say so.
"""


def generate_ap_comparison(teacher_questions):
    """Generate AP-aligned rubrics and compare with the teacher's rubric.

    `teacher_questions` is a list of {q, question_text, reference_solution,
    max_score, criteria}. Returns a dict with per-question AP rubrics,
    comparisons, and recommendations. Uses the reasoning model.
    """
    if not teacher_questions:
        return {"questions": [], "overall": "No questions to compare."}
    summary = []
    for q in teacher_questions:
        summary.append(
            {
                "q": q.get("q", ""),
                "question_text": str(q.get("question_text", ""))[:500],
                "reference_solution": str(q.get("reference_solution", ""))[:500],
                "max_score": q.get("max_score", 0),
                "criteria": str(q.get("criteria", ""))[:300],
            }
        )
    prompt = (
        "Teacher's rubric (from their hand-graded example):\n"
        + json.dumps(summary, indent=1)
        + "\n\nGenerate AP-style rubrics and compare."
    )
    try:
        data = _chat_json(
            AP_RUBRIC_SYSTEM,
            [{"type": "text", "text": prompt}],
            max_tokens=4000,
            model=REASONING_MODEL,
        )
    except Exception as e:
        return {"questions": [], "overall": f"AP comparison failed: {e}"}
    if not isinstance(data, dict):
        return {"questions": [], "overall": "AP comparison returned invalid data."}
    # Guard: AP totals must match the teacher's original max_score exactly.
    teacher_totals = {q.get("q", ""): int(q.get("max_score", 0) or 0) for q in teacher_questions}
    for aq in data.get("questions", []) or []:
        target = teacher_totals.get(aq.get("q", ""), None)
        if target is None:
            continue
        aq["teacher_total"] = target
        aq["ap_total"] = target
        pts = aq.get("ap_points") or []
        total = sum(int(p.get("points", 0) or 0) for p in pts if isinstance(p, dict))
        diff = target - total
        if diff != 0 and pts and isinstance(pts[-1], dict):
            pts[-1]["points"] = int(pts[-1].get("points", 0) or 0) + diff
    return data


def rubric_items_to_text(items):
    """Render the structured rubric as plain text (for teacher review / archiving)."""
    lines = []
    for it in items:
        ms = it.get("max_score", 0)
        ms_s = int(ms) if float(ms).is_integer() else ms
        # Accept multiple key variants for label/question/solution.
        label = it.get("q") or it.get("label") or it.get("question_label") or ""
        criteria = it.get("criteria", "")
        if isinstance(criteria, list):
            criteria = "; ".join(
                f"{c.get('points', 1)} pt: {c.get('criterion', c.get('description', ''))}"
                for c in criteria if isinstance(c, dict)
            )
        lines.append(f"{label} (max {ms_s} pts): {criteria}")
        qt = (it.get("question_text") or it.get("question") or "").strip()
        if qt:
            lines.append(f"  Question: {qt}")
        sol = (it.get("reference_solution") or it.get("solution") or "").strip()
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

    data = _chat_json(GRADER_SYSTEM, user_content, max_tokens=4096,
                      model="gpt-5.6-sol")

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
