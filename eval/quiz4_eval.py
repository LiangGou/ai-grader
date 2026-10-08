#!/usr/bin/env python3
"""Quiz #4 eval pipeline: rubric creation -> grading, using the 4 uploaded images.

Stage 1: Run the 4 images through build_unified_rubric_streaming (same as the
app). For each question, record: verified?, AP format?, points sum correct?
Stage 2: Grade the SAME 4 images as student papers against the created rubrics.
The teacher's handwritten work is correct, so expect full marks.

Outputs a JSON report to eval/quiz4/report.json.
"""
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/ai-grader")
os.chdir("/home/hatch/workspace/ai-grader")

import grader

EVAL_DIR = "/home/hatch/workspace/ai-grader/eval/quiz4"
IMAGES = [os.path.join(EVAL_DIR, f"quiz4_p{i}.jpg") for i in range(1, 5)]

# Expected max scores from the quiz paper: Q1=4, Q2=7, Q3=3, Q4=10, Q5=5, Q6=4, Q7=4, Q8=?, Q9=10
EXPECTED_MAX = {"1": 4, "2": 7, "3": 3, "4": 10, "5": 5, "6": 4, "7": 4, "9": 10}


def stage1_rubric_creation():
    print("=" * 60)
    print("STAGE 1: Rubric creation from 4 images")
    print("=" * 60)
    results = []
    items_out = []

    def on_item(item):
        items_out.append(item)
        q = item.get("q", "?")
        verified = item.get("verified")
        criteria = item.get("criteria", "")
        max_score = item.get("max_score", 0)
        ap_format = grader._is_ap_format(criteria)
        # Check points sum
        import re
        pts = [int(m) for m in re.findall(r"(\d+)\s*pt:", criteria)]
        pts_sum = sum(pts)
        pts_ok = pts_sum == int(max_score) if max_score else False
        print(f"  Q{q}: verified={verified}, ap_format={ap_format}, "
              f"pts_sum={pts_sum}/{max_score} ok={pts_ok}")
        results.append({
            "q": q,
            "verified": verified,
            "verify_note": item.get("verify_note", ""),
            "ap_format": ap_format,
            "points_sum": pts_sum,
            "max_score": max_score,
            "points_ok": pts_ok,
            "criteria_preview": criteria[:200],
            "origin": item.get("origin", ""),
        })

    t0 = time.time()
    try:
        result = grader.build_unified_rubric_streaming(
            IMAGES, upload_dir="/tmp/eval_uploads", on_item=on_item)
        # Consume the generator
        if hasattr(result, "__next__"):
            for _ in result:
                pass
    except Exception as e:
        print(f"  STAGE 1 FAILED: {e}")
        import traceback
        traceback.print_exc()
        return {"error": str(e), "questions": results}

    dt = time.time() - t0
    verified_count = sum(1 for r in results if r["verified"] == "yes")
    ap_count = sum(1 for r in results if r["ap_format"])
    pts_ok_count = sum(1 for r in results if r["points_ok"])
    print(f"\nStage 1 done in {dt:.1f}s: {len(results)} questions, "
          f"{verified_count} verified, {ap_count} AP format, {pts_ok_count} points OK")
    return {
        "duration_s": round(dt, 1),
        "total": len(results),
        "verified": verified_count,
        "ap_format": ap_count,
        "points_ok": pts_ok_count,
        "questions": results,
        "items": items_out,
    }


def stage2_grading(rubric_items):
    print("\n" + "=" * 60)
    print("STAGE 2: Grading the same 4 images as student papers")
    print("=" * 60)
    if not rubric_items:
        return {"error": "no rubrics from stage 1"}

    rubric_text = grader.rubric_items_to_text(rubric_items)
    results = []
    t0 = time.time()
    try:
        result = grader.grade_paper(rubric_text, rubric_items, IMAGES, [])
    except Exception as e:
        print(f"  STAGE 2 FAILED: {e}")
        import traceback
        traceback.print_exc()
        return {"error": str(e), "questions": results}

    dt = time.time() - t0
    questions = result.get("questions", []) if isinstance(result, dict) else []
    for q in questions:
        label = q.get("q", "?")
        score = q.get("score", 0)
        max_s = q.get("max_score", 0)
        # Teacher's work is correct -> expect full marks
        expected = EXPECTED_MAX.get(str(label), max_s)
        match = abs(float(score or 0) - float(expected or 0)) < 0.01
        print(f"  Q{label}: scored {score}/{max_s}, expected {expected}, match={match}")
        if not match:
            print(f"    feedback: {str(q.get('feedback', ''))[:150]}")
        results.append({
            "q": label,
            "score": score,
            "max_score": max_s,
            "expected": expected,
            "match": match,
            "feedback": str(q.get("feedback", ""))[:300],
        })

    matched = sum(1 for r in results if r["match"])
    print(f"\nStage 2 done in {dt:.1f}s: {matched}/{len(results)} scores match expected")
    return {
        "duration_s": round(dt, 1),
        "total": len(results),
        "matched": matched,
        "questions": results,
    }


def main():
    report = {"quiz": "Quiz #4", "images": IMAGES}
    s1 = stage1_rubric_creation()
    report["stage1_rubric"] = {k: v for k, v in s1.items() if k != "items"}
    s2 = stage2_grading(s1.get("items", []))
    report["stage2_grading"] = s2

    out_path = os.path.join(EVAL_DIR, "report.json")
    with open(out_path, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\nReport written to {out_path}")

    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    s1r = report["stage1_rubric"]
    if "error" not in s1r:
        print(f"Rubric: {s1r['verified']}/{s1r['total']} verified, "
              f"{s1r['ap_format']}/{s1r['total']} AP format, "
              f"{s1r['points_ok']}/{s1r['total']} points OK")
    s2r = report["stage2_grading"]
    if "error" not in s2r:
        print(f"Grading: {s2r['matched']}/{s2r['total']} scores correct")


if __name__ == "__main__":
    main()
