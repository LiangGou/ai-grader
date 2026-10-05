"""Call the OpenAI API for three jobs:
1. Turn the teacher's colloquial voice transcript into a structured rubric;
2. Grade photos of a student's paper question-by-question against the rubric;
3. Transcribe recorded voice memos with Whisper (record-then-transcribe flow).

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

STRUCTURE_SYSTEM = """You are an experienced grading lead who turns a teacher's spoken grading rules into a clear, structured rubric.
The teacher gives you a voice transcript (possibly colloquial, with repetitions and filler words). Extract, for each question: the question label, the max score, and the scoring/deduction rules.
Output ONLY a JSON object, no explanations, in this format:
{"items": [{"q": "Question 1", "max_score": 5, "criteria": "3 points for the correct concept, 2 points for complete steps"}, ...]}
If the transcript does not state a max score for a question, assign a reasonable default based on the question type and note "Max score set by AI default — please confirm" in criteria.
If the transcript yields no questions at all, output {"items": []}.
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


def transcribe_audio(path):
    """Transcribe a recorded voice memo with OpenAI Whisper. Returns the transcript string.

    Raises RuntimeError for missing API key (caller maps to 400); other API
    errors propagate for the caller to report.
    """
    client = _get_client()
    with open(path, "rb") as f:
        text = client.audio.transcriptions.create(
            model="whisper-1",
            file=f,
            language="en",
            response_format="text",
        )
    return (text or "").strip()


def structure_rubric(transcript):
    """Turn a colloquial transcript into a structured rubric. Returns {"items": [...]}."""
    data = _chat_json(
        STRUCTURE_SYSTEM,
        f"The teacher's spoken grading rules:\n\n{transcript}",
        max_tokens=2048,
    )
    items = data.get("items", [])
    norm = []
    for it in items:
        try:
            ms = float(it.get("max_score", 0))
        except (TypeError, ValueError):
            ms = 0
        norm.append(
            {
                "q": str(it.get("q", "")).strip(),
                "max_score": ms,
                "criteria": str(it.get("criteria", "")).strip(),
            }
        )
    return {"items": norm}


def rubric_items_to_text(items):
    """Render the structured rubric as plain text (for teacher review / archiving)."""
    lines = []
    for it in items:
        ms = it.get("max_score", 0)
        ms_s = int(ms) if float(ms).is_integer() else ms
        lines.append(f"{it.get('q', '')} (max {ms_s} pts): {it.get('criteria', '')}")
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
