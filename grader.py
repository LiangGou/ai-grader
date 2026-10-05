"""调用 OpenAI API（chat completions + vision）做两件事：
1. 把老师口语化的语音 transcript 整理成结构化 rubric；
2. 按 rubric 对学生答卷照片逐题判分。

图片以 base64 data URL 传入，支持多张答卷图 + 可选参考答案图。
"""
import base64
import json
import os
import re

from dotenv import load_dotenv

load_dotenv()

DEFAULT_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o")

STRUCTURE_SYSTEM = """你是一位经验丰富的阅卷组长，擅长把老师口头描述的判分规则整理成清晰的结构化评分标准。
老师会给你一段语音转写文本（可能口语化、有重复、有语气词），你要提炼出每一道题的：题号、满分、得分/扣分规则。
只输出 JSON 对象，不要任何解释文字，格式如下：
{"items": [{"q": "第一题", "max_score": 5, "criteria": "答对核心概念得3分，解题步骤完整得2分"}, ...]}
如果 transcript 里没有明确提到某题的满分，根据题目类型给一个合理的默认满分并在 criteria 里注明"满分由AI默认设定，请老师确认"。
如果 transcript 完全无法解析出任何题目，输出 {"items": []}。
"""

GRADER_SYSTEM = """你是一位严格按 rubric 判分的阅卷老师。规则：
1. 只根据图片中实际看到的学生作答判分。某题看不到、看不清、或学生未作答，该题给 0 分并在 feedback 中注明原因。
2. 每题得分不得超过该题满分。
3. 先简述每道题你识别到的学生答案（原文引用或概括），再依据 rubric 给分——这能减少幻觉。
4. 只输出 JSON 对象，不要任何解释文字，格式如下：
{"questions": [{"q": "题号", "score": 得分数字, "max_score": 满分数字, "answer_seen": "识别到的学生答案简述", "feedback": "给分/扣分理由"}],
 "total": 总分数字, "max_total": 满分总分数字, "overall_feedback": "整体评语"}
total 必须是各题 score 之和，max_total 必须是各题 max_score 之和。
"""


def _get_client():
    try:
        from openai import OpenAI
    except ImportError as e:
        raise RuntimeError("缺少 openai SDK，请先 pip install -r requirements.txt") from e
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("尚未配置 OPENAI_API_KEY，请先到「设置」页填写 API Key。")
    return OpenAI(api_key=api_key)


def _parse_json_loose(text):
    """解析模型输出的 JSON：先直接解析，失败则去 markdown 包裹/截取大括号兜底。"""
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
    raise ValueError("模型返回的内容不是合法 JSON")


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
        raise ValueError("模型返回为空，请重试。")
    return _parse_json_loose(text)


def structure_rubric(transcript):
    """把口语化 transcript 整理成结构化 rubric，返回 dict：{"items": [...]}。"""
    data = _chat_json(
        STRUCTURE_SYSTEM,
        f"老师的口头判分规则如下：\n\n{transcript}",
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
    """把结构化 rubric 拼成纯文本版（给老师看 / 存档）。"""
    lines = []
    for it in items:
        ms = it.get("max_score", 0)
        ms_s = int(ms) if float(ms).is_integer() else ms
        lines.append(f"{it.get('q', '')}（满分{ms_s}分）：{it.get('criteria', '')}")
    return "\n".join(lines)


def build_rubric_prompt_text(rubric_text, rubric_items):
    """判分时用的 rubric 文本：优先用结构化版本（更准）。"""
    if rubric_items:
        return rubric_items_to_text(rubric_items)
    return rubric_text or ""


def grade_paper(rubric_text, rubric_items, image_paths, reference_image_paths=None):
    """判分。返回 dict：{"questions": [...], "total": x, "max_total": y, "overall_feedback": ...}。"""
    if not image_paths:
        raise RuntimeError("没有收到答卷照片，请先拍照上传。")
    rubric_block = build_rubric_prompt_text(rubric_text, rubric_items)
    if not rubric_block.strip():
        raise RuntimeError("该任务还没有设置 rubric，请先到任务设置页录入评分规则。")

    user_content = []
    for p in reference_image_paths or []:
        user_content.append(
            {"type": "image_url", "image_url": {"url": _data_url(p)}}
        )
    ref_note = "上面是参考答案/标准答案，" if reference_image_paths else ""
    user_content.append(
        {
            "type": "text",
            "text": f"{ref_note}下面是学生的答卷照片。请按以下 rubric 逐题判分：\n\n{rubric_block}",
        }
    )
    for p in image_paths:
        user_content.append(
            {"type": "image_url", "image_url": {"url": _data_url(p)}}
        )

    data = _chat_json(GRADER_SYSTEM, user_content, max_tokens=4096)

    # 规范化 + 兜底校验
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
        s = max(0, min(s, max_s))  # 不超过满分、不为负
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
