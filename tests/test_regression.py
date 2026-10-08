#!/usr/bin/env python3
"""Regression test pipeline for the AI Grader rubric generation.

Uses fixed test images to run the full unified workflow on the backend,
validating that AP-first generation, retries, and guardrails work correctly.

Usage:
    cd ~/workspace/ai-grader && ./tests/test_regression.py [--quick]

    --quick: run only structural checks (no API calls, uses cached output)
    (default): full run with API calls (~2-5 min)

The test images are fixed; every code change should pass this suite before
being considered safe.
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Fixed test images (do not change these between runs).
TEST_IMAGES = [
    # Image with teacher worked solutions (example path).
    "uploads/rubric_example_1791401124_5032.jpg",
    # Add more as they are validated.
]

CACHE_FILE = "/tmp/regression_cache.json"


def check_item_structure(item, errors):
    """Every item must have the required fields with sane types."""
    required = ["q", "question_text", "reference_solution", "max_score",
                "criteria", "verified", "origin"]
    for f in required:
        if f not in item:
            errors.append(f"{item.get('q', '?')}: missing field '{f}'")
    if not isinstance(item.get("max_score", 0), (int, float)) or item.get("max_score", 0) <= 0:
        errors.append(f"{item.get('q', '?')}: invalid max_score={item.get('max_score')}")
    if item.get("verified") not in ("yes", "review"):
        errors.append(f"{item.get('q', '?')}: invalid verified={item.get('verified')}")
    if item.get("origin") not in ("teacher_example", "ai_solved"):
        errors.append(f"{item.get('q', '?')}: invalid origin={item.get('origin')}")


def check_ap_compliance(item, errors):
    """AP-first compliance.

    - ai_solved: criteria MUST be AP format ("N pt: Step M: ..."), steps
      sequential, sum == max_score.
    - teacher_example: teacher's own markings are kept as-is (not forced into
      AP format). Only check it's non-empty when verified=yes.
    """
    q = item.get("q", "?")
    criteria = item.get("criteria", "")
    max_score = item.get("max_score", 0) or 0

    if item.get("origin") == "teacher_example":
        # Teacher's markings are authoritative; just need something usable.
        if item.get("verified") == "yes" and len(criteria.strip()) < 10:
            errors.append(f"{q}: teacher_example verified but criteria too short")
        return

    # ai_solved: strict AP format required.
    if not criteria.strip():
        if item.get("verified") == "yes":
            errors.append(f"{q}: empty criteria but marked verified")
        return

    # Parse "N pt: Step M: ..." segments.
    pts = []
    for seg in criteria.split(";"):
        seg = seg.strip()
        m = re.match(r"(\d+)\s*pt\s*:\s*(.*)", seg, re.IGNORECASE)
        if m:
            pts.append((int(m.group(1)), m.group(2).strip()))
    if not pts:
        errors.append(f"{q}: criteria has no parseable 'N pt:' segments")
        return

    # 1. Sum must equal max_score.
    total = sum(p for p, _ in pts)
    if total != int(round(max_score)):
        errors.append(f"{q}: points sum to {total}, expected {int(round(max_score))}")

    # 2. Each point must reference a step.
    step_nums = []
    for _, text in pts:
        m = re.search(r"Step\s*(\d+)", text, re.IGNORECASE)
        if not m:
            errors.append(f"{q}: point does not reference a step: '{text[:60]}'")
        else:
            step_nums.append(int(m.group(1)))

    # 3. Steps must be sequential from 1.
    if step_nums and sorted(step_nums) != list(range(1, len(step_nums) + 1)):
        errors.append(f"{q}: step numbers not sequential: {sorted(step_nums)}")

    # 4. Granularity: 4+ point questions need 2+ points.
    if int(round(max_score)) >= 4 and len(pts) < 2:
        errors.append(f"{q}: {int(round(max_score))}-pt question has only {len(pts)} point(s)")


def check_solution_follows_plan(item, errors):
    """For ai_solved items, the solution must contain the step labels."""
    if item.get("origin") != "ai_solved":
        return
    q = item.get("q", "?")
    sol = item.get("reference_solution", "")
    if len(sol.strip()) < 50:
        errors.append(f"{q}: ai_solved solution too short ({len(sol.strip())} chars)")
        return
    # Extract step numbers from criteria, check each appears in solution.
    for seg in item.get("criteria", "").split(";"):
        m = re.search(r"Step\s*(\d+)", seg, re.IGNORECASE)
        if m:
            num = int(m.group(1))
            if not re.search(rf"Step\s*{num}\b", sol, re.IGNORECASE):
                errors.append(f"{q}: solution missing Step {num} from rubric")


def run_full_test():
    """Run the unified pipeline on test images and validate output."""
    import grader

    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    paths = [os.path.join(base, p) for p in TEST_IMAGES]
    missing = [p for p in paths if not os.path.exists(p)]
    if missing:
        print(f"FATAL: test images missing: {missing}")
        return False

    print(f"Running unified pipeline on {len(paths)} image(s)...")
    items = []

    def on_item(item):
        items.append(item)
        print(f"  [{item['q']}] origin={item.get('origin')} verified={item.get('verified')} "
              f"max={item.get('max_score')}")

    result = grader.build_unified_rubric_streaming(paths, max_workers=2, on_item=on_item)
    items = result["items"]

    # Cache for --quick mode.
    with open(CACHE_FILE, "w") as f:
        json.dump(items, f, indent=2)

    return validate_items(items)


def validate_items(items):
    """Run all checks on items (from live run or cache)."""
    errors = []
    if not items:
        errors.append("No items produced!")
    for item in items:
        check_item_structure(item, errors)
        check_ap_compliance(item, errors)
        check_solution_follows_plan(item, errors)

    print(f"\n{'='*50}")
    print(f"Checked {len(items)} item(s): {len(errors)} error(s)")
    for e in errors:
        print(f"  ❌ {e}")
    if not errors:
        print("  ✅ All checks passed!")
    print(f"{'='*50}")
    return len(errors) == 0


def test_ap_first_solve(errors):
    """Test the AP-first two-phase flow directly (no image needed).

    Verifies: Phase 1 generates plan from question, Phase 2 follows plan,
    validation catches bad output.
    """
    import grader

    print("Testing AP-first solve (text-only)...")
    try:
        result = grader.solve_and_rubric(
            "Differentiate $f(x) = x^3 + 2x^2 - 5x + 1$.",
            max_score=4,
            q_label="Q1",
        )
    except Exception as e:
        errors.append(f"AP-first solve raised: {e}")
        return

    sol = result.get("solution", "")
    pts = result.get("ap_points", [])

    # 1. Solution must have numbered steps.
    steps = re.findall(r"Step\s*(\d+)", sol, re.IGNORECASE)
    if not steps:
        errors.append("AP-first: solution has no numbered steps")
    # 2. Points must reference steps.
    for i, p in enumerate(pts):
        if not re.search(r"Step\s*\d+", str(p.get("for", "")), re.IGNORECASE):
            errors.append(f"AP-first: point {i+1} doesn't reference a step")
    # 3. Sum must equal max_score.
    total = sum(int(p.get("points", 0) or 0) for p in pts if isinstance(p, dict))
    if total != 4:
        errors.append(f"AP-first: points sum to {total}, expected 4")
    # 4. Every rubric step must appear in solution.
    for p in pts:
        m = re.search(r"Step\s*(\d+)", str(p.get("for", "")), re.IGNORECASE)
        if m and not re.search(rf"Step\s*{m.group(1)}\b", sol, re.IGNORECASE):
            errors.append(f"AP-first: solution missing Step {m.group(1)}")

    print(f"  Solution steps: {sorted(set(steps))}, points: {len(pts)}, sum: {total}")


def test_unicode_sanitization(errors):
    """No U+FFFD replacement chars in output (reported 2026-10-07: �1� mojibake).

    The model sometimes emits corrupted circled digits like �1� (meant ①).
    normalize_latex must recover the digit and drop stray replacement chars.
    """
    import grader

    print("Testing Unicode sanitization...")
    # Corrupted circled digits → recover the digit.
    out = grader.normalize_latex("we have\n$$\n\ufffd1\ufffd\n$$\nTherefore")
    if "\ufffd" in out:
        errors.append("Sanitization: U+FFFD not removed")
    if "1" not in out:
        errors.append("Sanitization: digit not recovered from \ufffd1\ufffd")
    # Stray replacement chars → dropped.
    out2 = grader.normalize_latex("Step 2: test \ufffd content \ufffd here")
    if "\ufffd" in out2:
        errors.append("Sanitization: stray U+FFFD not dropped")
    # Normal text untouched.
    out3 = grader.normalize_latex("Solve $x^2 = 4$ for $x$.")
    if out3 != "Solve $x^2 = 4$ for $x$.":
        errors.append(f"Sanitization: altered clean text: {out3!r}")
    print("  Sanitization checks done")


def test_is_ap_format(errors):
    """_is_ap_format must distinguish AP step rubrics from old 'deduct' style
    (needed for the Upgrade-to-AP flow, 2026-10-07)."""
    import grader

    print("Testing _is_ap_format...")
    ap = ("1 pt: Step 1: identifies the quotient rule; "
          "1 pt: Step 2: differentiates numerator and denominator; "
          "2 pt: Step 3: simplifies to final answer")
    if not grader._is_ap_format(ap):
        errors.append("_is_ap_format: rejected valid AP criteria")
    old_style = "deduct 2 points for missing or incorrect units"
    if grader._is_ap_format(old_style):
        errors.append("_is_ap_format: accepted old 'deduct' style as AP")
    if grader._is_ap_format(""):
        errors.append("_is_ap_format: accepted empty criteria as AP")
    if grader._is_ap_format(None):
        errors.append("_is_ap_format: accepted None as AP")
    print("  _is_ap_format checks done")


def test_duplicate_assignment_rejected(errors):
    """Duplicate assignment names in the same class must be rejected
    (reported 2026-10-07: double-tap created two 'T5's)."""
    import db

    print("Testing duplicate assignment rejection...")
    # Use a temp class so we don't touch real data.
    cid = db.create_class("TEST-DUP-CLASS")
    try:
        aid1 = db.create_assignment(cid, "TEST-DUP-NAME")
        assert aid1, "first create failed"
        # Second create with same name in same class must raise.
        try:
            db.create_assignment(cid, "TEST-DUP-NAME")
            errors.append("Duplicate: second create_assignment did not raise")
        except ValueError:
            pass  # expected
        # Same name in a DIFFERENT class is fine.
        cid2 = db.create_class("TEST-DUP-CLASS-2")
        try:
            aid2 = db.create_assignment(cid2, "TEST-DUP-NAME")
            assert aid2, "cross-class create failed"
        except ValueError:
            errors.append("Duplicate: rejected same name in different class")
        finally:
            _cleanup_class(cid2)
    finally:
        _cleanup_class(cid)
    print("  Duplicate checks done")


def _cleanup_class(cid):
    import sqlite3
    conn = sqlite3.connect(
        __import__("os").path.join(
            __import__("os").path.dirname(
                __import__("os").path.abspath(__file__)), "..", "ai_grader.db"))
    cur = conn.cursor()
    cur.execute("DELETE FROM assignments WHERE class_id=?", (cid,))
    cur.execute("DELETE FROM classes WHERE id=?", (cid,))
    conn.commit()
    conn.close()


def test_validation_guardrails(errors):
    """Test _check_ap_output catches bad output (no API calls)."""
    import grader

    print("Testing validation guardrails...")
    # Bad: references non-existent step.
    _, issues = grader._check_ap_output(
        "Step 1: do this.\nStep 2: do that.",
        [{"points": 2, "for": "Step 1: ..."}, {"points": 2, "for": "Step 5: ..."}],
        4, strict_steps=True)
    if not any("Step 5" in x for x in issues):
        errors.append("Guardrail: didn't catch reference to non-existent Step 5")
    # Bad: sum mismatch.
    _, issues = grader._check_ap_output(
        "Step 1: ...\nStep 2: ...",
        [{"points": 1, "for": "Step 1: ..."}],
        5, strict_steps=False)
    if not any("sum to" in x for x in issues):
        errors.append("Guardrail: didn't catch sum mismatch (1 vs 5)")
    # Good: should pass.
    _, issues = grader._check_ap_output(
        "Step 1: Write down the quotient rule formula for derivatives.\n"
        "Step 2: Differentiate the numerator and denominator separately.\n"
        "Step 3: Simplify the resulting expression to get the final answer.",
        [{"points": 1, "for": "Step 1: ..."}, {"points": 2, "for": "Step 2: ..."},
         {"points": 1, "for": "Step 3: ..."}],
        4, strict_steps=True)
    if issues:
        errors.append(f"Guardrail: false positive on good output: {issues[:1]}")
    print("  Guardrail checks done")


def test_frontend_logic(errors):
    """Run the Node-based frontend regression tests."""
    import subprocess
    print("Testing frontend logic (Node)...")
    test_js = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "test_frontend.js")
    try:
        r = subprocess.run(["node", test_js], capture_output=True, text=True,
                           timeout=30)
        print(r.stdout)
        if r.returncode != 0:
            errors.append(f"Frontend tests failed:\n{r.stdout}\n{r.stderr}")
    except Exception as e:
        errors.append(f"Could not run frontend tests: {e}")


def main():
    quick = "--quick" in sys.argv
    errors = []

    # Always run the no-API guardrail tests.
    test_validation_guardrails(errors)
    test_unicode_sanitization(errors)
    test_duplicate_assignment_rejected(errors)
    test_is_ap_format(errors)
    # Frontend JS logic tests (no API).
    test_frontend_logic(errors)

    if quick:
        if not os.path.exists(CACHE_FILE):
            print(f"No cache at {CACHE_FILE}; run without --quick first.")
            return False
        with open(CACHE_FILE) as f:
            items = json.load(f)
        print(f"Quick mode: validating {len(items)} cached item(s)...")
        ok = validate_items(items)
        return ok and len(errors) == 0
    else:
        # Need API key.
        if not os.environ.get("OPENAI_API_KEY"):
            env_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
            if os.path.exists(env_path):
                with open(env_path) as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith("OPENAI_API_KEY="):
                            os.environ["OPENAI_API_KEY"] = line.split("=", 1)[1]
        # AP-first solve test (uses API).
        test_ap_first_solve(errors)
        # Full pipeline test (uses API).
        ok = run_full_test()
        if errors:
            print(f"\nAdditional errors: {len(errors)}")
            for e in errors:
                print(f"  ❌ {e}")
        return ok and len(errors) == 0


if __name__ == "__main__":
    ok = main()
    sys.exit(0 if ok else 1)
