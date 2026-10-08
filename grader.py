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
- "reference_solution": the teacher's correct solution / working / final answer, transcribed faithfully, with mathematics in LaTeX as above (every fragment wrapped in $...$). Capture the final answer at minimum; include key working steps if shown, separating distinct steps with a line break.
- "max_score": the max points for the question.
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


def _chat_json(system, user_content, max_tokens, model=None, reasoning_effort=None, _raw=None):
    client = _get_client()
    model = model or DEFAULT_MODEL
    # Reasoning models (o1/o3) use max_completion_tokens and don't support
    # response_format=json_object; ask for JSON in the prompt instead.
    is_reasoning = model.startswith("o1") or model.startswith("o3")
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
    resp = client.chat.completions.create(**kwargs)
    text = (resp.choices[0].message.content or "").strip()
    if _raw is not None:
        _raw.append({"model": model, "effort": reasoning_effort, "raw": text})
    if not text:
        raise ValueError("The model returned an empty response. Please try again.")
    return _parse_json_loose(text)


def _digits_of(text):
    """Extract float numbers from a string for numeric comparison."""
    return [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", text or "")]


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
    # The model types literal \n (backslash + n) for line breaks between
    # formulas. KaTeX has no \n command, so it renders red. Turn it into a
    # real newline (whitespace outside math, harmless inside).
    text = text.replace("\\n", "\n")
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
        spans.append(m.group(0))
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


def _localize_diagram(image_path, question_text):
    """Locate a diagram's bounding box using grid-overlay visual prompting.

    Overlays a labeled 0-1000 coordinate grid on the photo and makes a
    dedicated vision call whose ONLY job is localization (far more accurate
    than asking for coordinates in the busy digitization pass).
    Returns (x_min, y_min, x_max, y_max) in 0-1000 coords, or None.
    """
    from PIL import Image, ImageDraw

    try:
        im = Image.open(image_path).convert("RGB")
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
    try:
        data = _chat_json(
            DIAGRAM_GEN_SYSTEM,
            [{"type": "text", "text": prompt}],
            max_tokens=2000,
            model=REASONING_MODEL,
        )
        code = data.get("code", "") if isinstance(data, dict) else ""
        if not code or not isinstance(code, str):
            # The model may return the code under a different key or as raw text.
            code = str(data) if data else ""
        # Strip markdown fences if the model added them despite instructions.
        code = re.sub(r"^```(?:python)?\s*", "", code.strip())
        code = re.sub(r"\s*```$", "", code.strip())
    except Exception:
        return None
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
    return {"kind": "image", "url": f"/uploads/{name}"}


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
        im = Image.open(src).convert("RGB")
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


def _process_graph(question, image_paths, upload_dir):
    """Turn a digitized diagram into render-ready data (or None).

    Primary: generate the diagram with Python/matplotlib via the reasoning
    model (it understands the problem context and solves as needed).
    Fallbacks: legacy function-expression sampling, then photo crop.
    """
    if not question.get("has_diagram"):
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
                "has_diagram": bool(it.get("has_diagram", False)),
                "diagram_hint": str(it.get("diagram_hint", "")).strip(),
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
        return {
            "idx": idx,
            "q": q["q"],
            "question_text": q["question_text"],
            "reference_solution": q["reference_solution"],
            "max_score": q["max_score"],
            "criteria": q["criteria"],
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


SOLVE_RUBRIC_SYSTEM = """You are an expert AP Calculus teacher creating an answer key and scoring rubric from scratch.
Given a question and its point value:

1. SOLVE the question completely and correctly. Show every step. This is the answer key — it must be right.
2. CREATE an AP-style scoring rubric: break the total points into specific, observable criteria following AP free-response conventions:
   - Award points for specific steps (setup, correct mathematical work, final answer), not just the final answer.
   - Typical: 1 point for correct setup, 1-2 points for correct work/computation, 1 point for the final answer (with correct units or justification where applicable).
   - Each criterion must state exactly what earns the point.

CRITICAL: The point breakdown MUST sum to EXACTLY the given max_score. You are distributing exactly that many points — never more, never fewer.

Output ONLY a JSON object, no explanations:
{
  "solution": "<complete worked solution, mathematics in LaTeX with $...$ delimiters, steps separated by line breaks>",
  "ap_points": [{"points": 1, "for": "<what earns this point>"}, ...],
  "notes": "<any ambiguity in the question, or empty>"
}
"""


VERIFY_SOLUTION_SYSTEM = """You are a careful, independent checker. You are given a calculus question and a proposed solution (an AI-generated answer key).
Your job:
1. Solve the question yourself, step by step, WITHOUT trusting the proposed solution.
2. Compare your final answer with the proposed solution.
3. If the question is a numerical computation, also provide a single Python expression (using only arithmetic operators and the math module, e.g. "math.pi * 3**2") that computes the answer. Leave it empty if not numerical.
Output ONLY a JSON object, no explanations:
{"your_answer": "<your final answer, concise>", "agrees": true/false, "confidence": "high/medium/low", "note": "<one sentence on any discrepancy, or empty>", "python_check": "<expression or empty>"}
Set "agrees" to true only if you are confident the two answers match (allowing equivalent forms, e.g. 0.5 = 1/2).
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
    data = _chat_json(QUESTIONS_ONLY_SYSTEM, user_content, max_tokens=4096)
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
                   model=None, reasoning_effort=None, _debug=None):
    """Reasoning-model pass: solve the question and generate an AP-style rubric.

    Returns {solution, ap_points, notes}. The ap_points sum to exactly max_score.
    When image_paths is given, the model also sees the original photos so it
    can read the PRINTED question directly (guarding against transcription
    errors); it must still ignore all handwriting.
    """
    prompt = (
        f"Question {q_label} (worth {max_score:g} points):\n{question_text}\n\n"
        "Solve it and create the AP-style rubric."
    )
    user_content = [{"type": "text", "text": prompt}]
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
    data = _chat_json(
        SOLVE_RUBRIC_SYSTEM,
        user_content,
        max_tokens=4096,
        model=model or REASONING_MODEL,
        reasoning_effort=reasoning_effort,
        _raw=_debug,
    )
    pts = data.get("ap_points") or []
    # Guard: force the breakdown to sum to exactly max_score.
    target = int(round(max_score))
    total = sum(int(p.get("points", 0) or 0) for p in pts if isinstance(p, dict))
    diff = target - total
    if diff != 0 and pts and isinstance(pts[-1], dict):
        pts[-1]["points"] = int(pts[-1].get("points", 0) or 0) + diff
    return {
        "solution": normalize_latex(str(data.get("solution", "")).strip()),
        "ap_points": pts,
        "notes": str(data.get("notes", "")).strip(),
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
                         high_effort=False, image_paths=None):
    """Retry solving a single question, with full debug output.

    `model` is "o3" or "gpt-4o". `high_effort` requests o3 with
    reasoning_effort="high" (slower, more thorough).

    Returns {solution, criteria, verified, verify_note, debug} where debug is
    a list of {step, model, raw} showing exactly what each model call returned.
    """
    debug = []
    effort = "high" if high_effort and model.startswith("o") else None
    use_model = model or REASONING_MODEL

    solve_debug, verify_debug = [], []
    solved = solve_and_rubric(question_text, max_score, q_label,
                              image_paths=image_paths, model=use_model,
                              reasoning_effort=effort, _debug=solve_debug)
    for d in solve_debug:
        debug.append({"step": "solve", "model": d["model"], "effort": d["effort"], "raw": d["raw"]})
    verdict = verify_solution(question_text, solved["solution"], model=use_model,
                              reasoning_effort=effort, _debug=verify_debug)
    for d in verify_debug:
        debug.append({"step": "verify", "model": d["model"], "effort": d["effort"], "raw": d["raw"]})

    verified = "yes" if verdict["verdict"] == "confirmed" else "review"
    criteria = "; ".join(
        f"{p.get('points', 1)} pt: {p.get('for', '')}"
        for p in solved["ap_points"] if isinstance(p, dict)
    )
    return {
        "solution": solved["solution"],
        "criteria": criteria,
        "max_score": max_score,
        "verified": verified,
        "verify_note": verdict.get("note", ""),
        "debug": debug,
    }


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
            solved = solve_and_rubric(q.get("question_text", ""), max_score, q_label,
                                      image_paths=image_paths)
            verdict = verify_solution(q.get("question_text", ""), solved["solution"])
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
                "verify_note": f"AI solving failed ({e}). Please solve manually or retry.",
                "origin": "ai_solved",
                "graph": None,
            }
        verified = "yes" if verdict["verdict"] == "confirmed" else "review"
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
            "verify_note": verdict.get("note", ""),
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
