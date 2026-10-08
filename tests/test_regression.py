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


def test_normalize_latex_environments(errors):
    import grader

    print("Testing normalize_latex environment protection...")

    cases = [
        (
            "undelimited aligned",
            r"\begin{aligned}x &= 1 \\ y &= 2\end{aligned}",
            r"$$\begin{aligned}x &= 1 \\ y &= 2\end{aligned}$$",
        ),
        (
            "delimited aligned",
            r"$$\begin{aligned}x &= 1 \\ y &= 2\end{aligned}$$",
            r"$$\begin{aligned}x &= 1 \\ y &= 2\end{aligned}$$",
        ),
        (
            "undelimited align",
            r"\begin{align}a &= b+c \\ d &= e-f\end{align}",
            r"$$\begin{align}a &= b+c \\ d &= e-f\end{align}$$",
        ),
        (
            "delimited align",
            r"$$\begin{align}a &= b+c \\ d &= e-f\end{align}$$",
            r"$$\begin{align}a &= b+c \\ d &= e-f\end{align}$$",
        ),
        (
            "undelimited cases",
            r"\begin{cases}x^2 & x>0 \\ 0 & x\leq 0\end{cases}",
            r"$$\begin{cases}x^2 & x>0 \\ 0 & x\leq 0\end{cases}$$",
        ),
        (
            "delimited cases",
            r"$$\begin{cases}x^2 & x>0 \\ 0 & x\leq 0\end{cases}$$",
            r"$$\begin{cases}x^2 & x>0 \\ 0 & x\leq 0\end{cases}$$",
        ),
        (
            "single-dollar delimited environment",
            r"$\begin{aligned}x &= 1 \\ y &= 2\end{aligned}$",
            r"$\begin{aligned}x &= 1 \\ y &= 2\end{aligned}$",
        ),
    ]

    for name, value, expected in cases:
        out = grader.normalize_latex(value)
        if out != expected:
            errors.append(
                f"normalize_latex {name}: got {out!r}, expected {expected!r}"
            )

        if out.count(r"\begin") != 1 or out.count(r"\end") != 1:
            errors.append(
                f"normalize_latex {name}: environment was fragmented: {out!r}"
            )

    print("  done")

def test_normalize_latex_multiple_environments(errors):
    import grader

    print("Testing multiple protected LaTeX environments...")

    value = (
        r"\begin{aligned}x &= 1 \\ y &= 2\end{aligned}"
        " and "
        r"\begin{cases}1 & x>0 \\ 0 & x\leq 0\end{cases}"
    )
    expected = (
        r"$$\begin{aligned}x &= 1 \\ y &= 2\end{aligned}$$"
        " and "
        r"$$\begin{cases}1 & x>0 \\ 0 & x\leq 0\end{cases}$$"
    )

    out = grader.normalize_latex(value)
    if out != expected:
        errors.append(
            f"normalize_latex multiple environments: got {out!r}, "
            f"expected {expected!r}"
        )

    if out.count(r"\begin") != 2 or out.count(r"\end") != 2:
        errors.append(
            f"normalize_latex multiple environments were fragmented: {out!r}"
        )

    print("  done")

def test_normalize_latex_external_line_separators(errors):
    import grader

    print("Testing LaTeX separators outside protected environments...")

    cases = [
        (
            "commands on both lines",
            r"\alpha \\ \beta",
            "$\\alpha$\n$\\beta$",
        ),
        (
            "plain text on both lines",
            r"Before \\ after",
            "Before\nafter",
        ),
        (
            "separator following an environment",
            r"\begin{aligned}x &= 1 \\ y &= 2\end{aligned} \\ \gamma",
            (
                r"$$\begin{aligned}x &= 1 \\ y &= 2\end{aligned}$$"
                "\n"
                r"$\gamma$"
            ),
        ),
    ]

    for name, value, expected in cases:
        out = grader.normalize_latex(value)
        if out != expected:
            errors.append(
                f"normalize_latex external separator {name}: "
                f"got {out!r}, expected {expected!r}"
            )

    print("  done")

def test_balance_braces_edge_cases(errors):
    import grader

    print("Testing _balance_braces edge cases...")

    cases = [
        ("empty", "", ""),
        ("plain text", "plain text", "plain text"),
        ("stray closing brace", "(slope}=0)", "(slope=0)"),
        ("missing closing brace", r"\frac{1}{2", r"\frac{1}{2}"),
        ("escaped braces", r"\{a\}", r"\{a\}"),
        ("nested missing close", "{{x}", "{{x}}"),
        ("already balanced", r"\frac{a+b}{c-d}", r"\frac{a+b}{c-d}"),
        ("leading unmatched closes", "}}x", "x"),
        ("several missing closes", "{{{x", "{{{x}}}"),
        ("mixed unmatched braces", "}a{b}}c{", "a{b}c{}"),
        ("escaped open brace only", r"\{x", r"\{x"),
        ("escaped close brace only", r"x\}", r"x\}"),
        ("escaped and structural braces", r"{\{x\}}", r"{\{x\}}"),
        ("multiple balanced groups", "{a}{b}{c}", "{a}{b}{c}"),
    ]

    for name, value, expected in cases:
        out = grader._balance_braces(value)
        if out != expected:
            errors.append(
                f"_balance_braces {name}: got {out!r}, expected {expected!r}"
            )

    print("  done")

def test_normalize_latex_brace_repair(errors):
    import grader

    print("Testing normalize_latex brace repair...")

    cases = [
        ("stray close", "(slope}=0)", "(slope=0)"),
        ("missing fraction close", r"\frac{1}{2", r"$\frac{1}{2}$"),
        ("escaped braces", r"\{a\}", r"$\{a\}$"),
        (
            "brace repair inside environment",
            r"\begin{aligned}x &= \frac{1}{2 \\ y &= 3\end{aligned}",
            r"$$\begin{aligned}x &= \frac{1}{2 \\ y &= 3\end{aligned}}$$",
        ),
    ]

    for name, value, expected in cases:
        out = grader.normalize_latex(value)
        if out != expected:
            errors.append(
                f"normalize_latex brace repair {name}: "
                f"got {out!r}, expected {expected!r}"
            )

    print("  done")

def test_normalize_latex_idempotency(errors):
    import grader

    print("Testing normalize_latex idempotency...")

    inputs = [
        "",
        "Plain English passes through unchanged.",
        r"\frac{1}{2",
        r"\alpha \\ \beta",
        r"\begin{aligned}x &= 1 \\ y &= 2\end{aligned}",
        r"$$\begin{cases}1 & x>0 \\ 0 & x\leq 0\end{cases}$$",
        r"$\[x+1\]$",
        "\ufffd1\ufffd and \ufffd2\ufffd",
    ]

    for value in inputs:
        once = grader.normalize_latex(value)
        twice = grader.normalize_latex(once)
        if twice != once:
            errors.append(
                f"normalize_latex is not idempotent for {value!r}: "
                f"once={once!r}, twice={twice!r}"
            )

    print("  done")

def test_normalize_latex_misc_edge_cases(errors):
    import grader

    print("Testing miscellaneous normalize_latex edge cases...")

    cases = [
        ("empty string", "", ""),
        ("None", None, None),
        ("plain English", "No mathematical notation here.", "No mathematical notation here."),
        ("literal newline escape", r"first\nsecond", "first\nsecond"),
        ("mixed display delimiters", r"$\[x+1\]$", "$$x+1$$"),
        ("odd lone dollar", "Cost is $5", "Cost is 5"),
        ("corrupted circled digit", "\ufffd1\ufffd", "1"),
        ("multiple corrupted digits", "\ufffd1\ufffd + \ufffd2\ufffd", "1 + 2"),
        ("stray replacement character", "ab\ufffdcd", "abcd"),
    ]

    for name, value, expected in cases:
        out = grader.normalize_latex(value)
        if out != expected:
            errors.append(
                f"normalize_latex {name}: got {out!r}, expected {expected!r}"
            )

    print("  done")

def test_step_number_word_boundaries(errors):
    import re

    print("Testing step-number word boundaries...")

    for num in range(1, 13):
        pat = r"Step\s*%d\b" % num

        positive = [
            f"Step {num}",
            f"step {num}: explanation",
            f"STEP  {num}",
            f"Step     {num} is complete",
            f"Use Step{num}.",
        ]
        for text in positive:
            if not re.search(pat, text, re.IGNORECASE):
                errors.append(
                    f"step regex {pat!r} should match {text!r}"
                )

        larger = num * 10
        negative = [
            f"Step {larger}",
            f"Step  {larger}: explanation",
            f"STEP{larger}",
        ]
        for text in negative:
            if re.search(pat, text, re.IGNORECASE):
                errors.append(
                    f"step regex {pat!r} incorrectly matched {text!r}"
                )

    explicit_negative_cases = [
        (1, "Step 10"),
        (1, "Step 11"),
        (1, "Step 100"),
        (2, "Step 20"),
        (2, "Step 21"),
        (2, "Step 200"),
        (12, "Step 120"),
    ]
    for num, text in explicit_negative_cases:
        pat = r"Step\s*%d\b" % num
        if re.search(pat, text, re.IGNORECASE):
            errors.append(
                f"step regex {pat!r} incorrectly matched {text!r}"
            )

    print("  done")

def test_part_step_regex_variants(errors):
    import re

    print("Testing Part/Step regex variants...")

    validation_pat = r"Part\s*\(a\)\s*Step\s*1\b"
    positive = [
        "Part (a) Step 1",
        "part(a)step1",
        "PART   (a)   STEP  1:",
        "Part(a) Step     1 is complete",
    ]
    for text in positive:
        if not re.search(validation_pat, text, re.IGNORECASE):
            errors.append(
                f"part-step validation regex should match {text!r}"
            )

    negative = [
        "Part (a) Step 10",
        "Part (a) Step 11",
        "Part (b) Step 1",
        "Part (a) Step 100",
    ]
    for text in negative:
        if re.search(validation_pat, text, re.IGNORECASE):
            errors.append(
                f"part-step validation regex incorrectly matched {text!r}"
            )

    parser = re.compile(
        r"(?:Part\s*\(([a-zA-Z])\)\s*)?Step\s*(\d+)",
        re.IGNORECASE,
    )
    parse_cases = [
        ("Step 1", (None, "1")),
        ("step   12", (None, "12")),
        ("Part (a) Step 1", ("a", "1")),
        ("part(B)step10", ("B", "10")),
        ("PART   (c)   STEP  12:", ("c", "12")),
    ]

    for text, expected_groups in parse_cases:
        match = parser.search(text)
        actual_groups = match.groups() if match else None
        if actual_groups != expected_groups:
            errors.append(
                f"step-reference parser for {text!r}: "
                f"got {actual_groups!r}, expected {expected_groups!r}"
            )

    print("  done")

def test_solve_rubric_route_exists(errors):
    print("Testing /api/solve_rubric route existence...")

    try:
        import app as app_module
        client = app_module.app.test_client()
    except Exception as exc:
        errors.append(f"could not create Flask test client: {exc!r}")
        print("  done")
        return

    requests = [
        ("missing image list", {}),
        ("empty image list", {"images": []}),
    ]

    for name, payload in requests:
        try:
            response = client.post("/api/solve_rubric", json=payload)
        except Exception as exc:
            errors.append(
                f"/api/solve_rubric {name} request raised {exc!r}"
            )
            continue

        if response.status_code == 404:
            errors.append(
                f"/api/solve_rubric {name}: route returned 404"
            )
            continue

        if response.status_code != 400:
            errors.append(
                f"/api/solve_rubric {name}: got status "
                f"{response.status_code}, expected 400"
            )

        body = response.get_json(silent=True)
        if not isinstance(body, dict):
            errors.append(
                f"/api/solve_rubric {name}: expected a JSON object, got {body!r}"
            )
        elif "error" not in body:
            errors.append(
                f"/api/solve_rubric {name}: JSON response lacks 'error': {body!r}"
            )

    print("  done")


def test_solve_with_retries_exception_then_success(errors):
    print("Testing _solve_with_retries exception recovery and retry_hint...")
    import grader
    from unittest.mock import patch

    solved = {
        "solution": "The answer is 4.",
        "rubric": [],
    }
    confirmed = {
        "verdict": "confirmed",
        "note": "Checked.",
    }

    try:
        with patch.object(
            grader,
            "solve_and_rubric",
            side_effect=[RuntimeError("temporary model failure"), solved],
        ) as mock_solve, patch.object(
            grader,
            "verify_solution",
            return_value=confirmed,
        ) as mock_verify:
            actual_solved, actual_verdict = grader._solve_with_retries(
                "What is 2 + 2?",
                2,
                q_label="1",
                image_paths=["page.png"],
                max_attempts=3,
            )

        if actual_solved != solved:
            errors.append(
                "_solve_with_retries did not return the successful retry result"
            )
        if actual_verdict != confirmed:
            errors.append(
                "_solve_with_retries did not return the successful verification verdict"
            )
        if mock_solve.call_count != 2:
            errors.append(
                f"_solve_with_retries made {mock_solve.call_count} solve calls; expected 2"
            )
        if mock_verify.call_count != 1:
            errors.append(
                f"verify_solution was called {mock_verify.call_count} times; expected 1"
            )

        calls = mock_solve.call_args_list
        if len(calls) >= 2:
            first_hint = calls[0].kwargs.get("retry_hint")
            second_hint = calls[1].kwargs.get("retry_hint")

            if first_hint != "":
                errors.append(
                    f"First solve attempt received unexpected retry_hint: {first_hint!r}"
                )
            if "Attempt 1" not in (second_hint or ""):
                errors.append(
                    f"Second solve attempt did not identify attempt 1: {second_hint!r}"
                )
            if "temporary model failure" not in (second_hint or ""):
                errors.append(
                    "Second solve attempt did not receive the previous exception text"
                )

            if calls[1].kwargs.get("image_paths") != ["page.png"]:
                errors.append("image_paths were not preserved across solve retries")
    except Exception as exc:
        errors.append(
            f"_solve_with_retries exception-recovery test raised unexpectedly: {exc}"
        )

    print("  done")

def test_solve_with_retries_stops_on_identical_solution(errors):
    print("Testing _solve_with_retries identical-solution early exit...")
    import grader
    from unittest.mock import patch

    first = {
        "solution": "  x = 7  ",
        "rubric": [],
    }
    second = {
        "solution": "x = 7",
        "rubric": [],
    }
    mismatch = {
        "verdict": "mismatch",
        "note": "Independent check disagreed.",
    }

    try:
        with patch.object(
            grader,
            "solve_and_rubric",
            side_effect=[first, second, second, second, second],
        ) as mock_solve, patch.object(
            grader,
            "verify_solution",
            return_value=mismatch,
        ) as mock_verify:
            solved, verdict = grader._solve_with_retries(
                "Solve for x.",
                3,
                q_label="2",
                max_attempts=5,
            )

        if mock_solve.call_count != 2:
            errors.append(
                "Identical normalized solutions should stop after two attempts; "
                f"got {mock_solve.call_count} attempts"
            )
        if mock_verify.call_count != 2:
            errors.append(
                f"Expected two verification calls, got {mock_verify.call_count}"
            )
        if solved != second:
            errors.append("The duplicate-answer path did not return the latest solution")
        if not isinstance(verdict, dict) or verdict.get("verdict") != "review":
            errors.append(
                f"Duplicate answers should force verdict='review'; got {verdict!r}"
            )

        note = (verdict or {}).get("note", "").lower()
        if "same answer twice" not in note:
            errors.append(
                f"Duplicate-answer review note is missing the explanation: {note!r}"
            )
        if "independent check disagreed" not in note:
            errors.append(
                "Duplicate-answer handling discarded the verifier's original note"
            )
    except Exception as exc:
        errors.append(
            f"_solve_with_retries identical-solution test raised unexpectedly: {exc}"
        )

    print("  done")

def test_build_unified_rubric_routes_teacher_and_ai_items(errors):
    print("Testing build_unified_rubric_streaming per-question routing...")
    import grader
    import inspect
    from unittest.mock import patch

    questions = [
        {
            "q_label": "1",
            "question_text": "TEACHER QUESTION",
            "reference_solution": "TEACHER SOLUTION",
            "rubric_criteria": "Award 1 point for the setup.",
            "max_score": 4,
        },
        {
            "q_label": "2",
            "question_text": "AI QUESTION",
            "reference_solution": "   ",
            "rubric_criteria": "",
            "max_score": 3,
        },
    ]

    teacher_item = {
        "q_label": "1",
        "question_text": "TEACHER QUESTION",
        "solution": "TEACHER SOLUTION",
        "rubric": [],
        "max_score": 4,
        "verified": "confirmed",
        "origin": "teacher_example",
    }
    ai_solved = {
        "solution": "AI SOLUTION",
        "rubric": [],
    }
    ai_verdict = {
        "verdict": "confirmed",
        "note": "Verified.",
    }

    def consume_stream(value):
        if inspect.isgenerator(value) or (
            hasattr(value, "__next__") and not isinstance(value, (str, bytes))
        ):
            while True:
                try:
                    next(value)
                except StopIteration:
                    break
        return value

    try:
        with patch.object(
            grader,
            "_build_from_example",
            return_value=teacher_item,
        ) as mock_example, patch.object(
            grader,
            "_solve_with_retries",
            return_value=(ai_solved, ai_verdict),
        ) as mock_solve:
            result = grader.build_unified_rubric_streaming(questions)
            consume_stream(result)

        if mock_example.call_count != 1:
            errors.append(
                "Expected exactly one teacher-example build, "
                f"got {mock_example.call_count}"
            )
        if mock_solve.call_count != 1:
            errors.append(
                f"Expected exactly one AI solve, got {mock_solve.call_count}"
            )

        if mock_example.call_args:
            example_text = repr(mock_example.call_args)
            if "TEACHER QUESTION" not in example_text:
                errors.append(
                    "Teacher-example branch was not called with the teacher question"
                )
            if "TEACHER SOLUTION" not in example_text:
                errors.append(
                    "Teacher-example branch did not receive the reference solution"
                )

        if mock_solve.call_args:
            solve_text = repr(mock_solve.call_args)
            if "AI QUESTION" not in solve_text:
                errors.append("AI-solve branch received the wrong question")
            if "TEACHER QUESTION" in solve_text:
                errors.append(
                    "Question with a teacher reference was incorrectly sent to AI solve"
                )
    except Exception as exc:
        errors.append(
            f"build_unified_rubric_streaming routing test raised unexpectedly: {exc}"
        )

    print("  done")

def test_build_unified_rubric_ai_fallback_and_default_score(errors):
    print("Testing rubric-build AI fallback and default max_score...")
    import grader
    import inspect
    import json
    from unittest.mock import patch

    questions = [
        {
            "q_label": "3",
            "question_text": "FALLBACK QUESTION",
            "reference_solution": "",
            "rubric_criteria": "",
            "max_score": 0,
        }
    ]

    def drain(value):
        chunks = []
        return_value = None

        if inspect.isgenerator(value) or (
            hasattr(value, "__next__") and not isinstance(value, (str, bytes))
        ):
            while True:
                try:
                    chunks.append(next(value))
                except StopIteration as stop:
                    return_value = stop.value
                    break
        else:
            chunks.append(value)

        if return_value is not None:
            chunks.append(return_value)
        return chunks

    try:
        with patch.object(
            grader,
            "_solve_with_retries",
            side_effect=RuntimeError("synthetic solve failure"),
        ) as mock_solve:
            result = grader.build_unified_rubric_streaming(questions)
            chunks = drain(result)

        if mock_solve.call_count != 1:
            errors.append(
                f"Expected one failed AI solve, got {mock_solve.call_count}"
            )

        if mock_solve.call_args:
            call = mock_solve.call_args
            score = call.args[1] if len(call.args) > 1 else call.kwargs.get("max_score")
            if score != 5:
                errors.append(
                    f"Missing/zero max_score should default to 5; solver received {score!r}"
                )

        blob_parts = []
        for chunk in chunks:
            if isinstance(chunk, str):
                blob_parts.append(chunk)
            else:
                blob_parts.append(json.dumps(chunk, default=str))
        blob = "\n".join(blob_parts).lower()

        if "fallback question" not in blob:
            errors.append("Failed AI question disappeared from the streamed fallback")
        if '"verified": "review"' not in blob and "'verified': 'review'" not in blob:
            errors.append("Failed AI solve was not marked verified='review'")
        if '"origin": "ai_solved"' not in blob and "'origin': 'ai_solved'" not in blob:
            errors.append("Failed AI solve did not retain origin='ai_solved'")
        if "max score set by ai default" not in blob:
            errors.append(
                "Default max-score confirmation note was not included in the fallback"
            )
    except Exception as exc:
        errors.append(
            f"AI fallback/default-score test raised unexpectedly: {exc}"
        )

    print("  done")

def test_parse_json_loose_variants(errors):
    print("Testing _parse_json_loose model-response variants...")
    import grader

    cases = [
        (
            '{"verdict":"confirmed","note":"ok"}',
            {"verdict": "confirmed", "note": "ok"},
        ),
        (
            '```json\n{"verdict":"review","note":"check"}\n```',
            {"verdict": "review", "note": "check"},
        ),
        (
            'Model preface.\n{"outer":{"value":3},"items":[1,2]}\nTrailing explanation.',
            {"outer": {"value": 3}, "items": [1, 2]},
        ),
        (
            '```\n{"max_score":5,"question_text":"Q1"}\n```',
            {"max_score": 5, "question_text": "Q1"},
        ),
    ]

    try:
        for raw, expected in cases:
            try:
                actual = grader._parse_json_loose(raw)
                if actual != expected:
                    errors.append(
                        f"_parse_json_loose({raw!r}) returned {actual!r}; "
                        f"expected {expected!r}"
                    )
            except Exception as exc:
                errors.append(
                    f"_parse_json_loose rejected valid input {raw!r}: {exc}"
                )

        invalid_cases = [
            "",
            "This response contains no JSON.",
            "```json\nnot actually json\n```",
            'prefix {"broken": true suffix',
        ]
        for raw in invalid_cases:
            try:
                grader._parse_json_loose(raw)
                errors.append(
                    f"_parse_json_loose accepted invalid input instead of raising: {raw!r}"
                )
            except ValueError:
                pass
            except Exception as exc:
                errors.append(
                    f"_parse_json_loose raised {type(exc).__name__}, expected ValueError "
                    f"for {raw!r}"
                )
    except Exception as exc:
        errors.append(f"_parse_json_loose test raised unexpectedly: {exc}")

    print("  done")

def test_verify_solution_python_check_confirmed_and_mismatch(errors):
    print("Testing verify_solution deterministic python_check verdicts...")
    import grader
    import json
    from types import SimpleNamespace
    from unittest.mock import patch

    payload = {
        "agrees": False,
        "confidence": "low",
        "python_check": "2 + 3 * 4",
        "note": "Use the deterministic arithmetic check.",
    }
    content = json.dumps(payload)

    class FakeCompletions:
        def create(self, *args, **kwargs):
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content=content)
                    )
                ]
            )

    class FakeResponses:
        def create(self, *args, **kwargs):
            return SimpleNamespace(
                output_text=content,
                output=[
                    SimpleNamespace(
                        content=[SimpleNamespace(text=content)]
                    )
                ],
            )

    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=FakeCompletions()),
        responses=FakeResponses(),
    )

    try:
        # Patch both common client names so this remains network-free whether
        # grader.py uses the chat-completions or responses interface.
        with patch.object(grader, "client", fake_client, create=True), \
             patch.object(grader, "openai_client", fake_client, create=True):
            matching = grader.verify_solution(
                "Evaluate 2 + 3 * 4.",
                "Using order of operations, the final answer is 14.",
            )
            mismatching = grader.verify_solution(
                "Evaluate 2 + 3 * 4.",
                "Using order of operations, the final answer is 15.",
            )

        if matching.get("verdict") != "confirmed":
            errors.append(
                "Matching python_check result should be confirmed; "
                f"got {matching!r}"
            )
        if mismatching.get("verdict") != "mismatch":
            errors.append(
                "Nonmatching python_check result should be mismatch; "
                f"got {mismatching!r}"
            )
    except Exception as exc:
        errors.append(
            "verify_solution python_check test raised unexpectedly "
            f"(and may have attempted a real API path): {exc}"
        )

    print("  done")

def test_safe_eval_and_digits_of_numeric_edge_cases(errors):
    print("Testing _safe_eval and _digits_of numeric behavior...")
    import grader

    try:
        eval_cases = [
            ("2 + 3 * 4", 14.0),
            ("(10 - 4) / 3", 2.0),
            ("-3 + 1.5", -1.5),
            ("2 ** 5", 32.0),
        ]

        for expression, expected in eval_cases:
            actual = grader._safe_eval(expression)
            try:
                numeric = float(actual)
            except (TypeError, ValueError):
                errors.append(
                    f"_safe_eval({expression!r}) returned nonnumeric value {actual!r}"
                )
                continue

            if abs(numeric - expected) > 1e-9:
                errors.append(
                    f"_safe_eval({expression!r}) returned {actual!r}; "
                    f"expected {expected!r}"
                )

        unsafe_cases = [
            "__import__('os').system('echo unsafe')",
            "(1).__class__",
            "[1, 2, 3][0]",
            "open('/tmp/unsafe', 'w')",
        ]
        for expression in unsafe_cases:
            try:
                actual = grader._safe_eval(expression)
                if actual is not None:
                    errors.append(
                        f"_safe_eval accepted unsafe expression {expression!r}: "
                        f"{actual!r}"
                    )
            except Exception:
                # Rejecting unsafe input by raising is also acceptable.
                pass

        extracted = grader._digits_of(
            "Answers: x = -3.5, y = 12, and the correction is +0.25."
        )
        numeric_values = []
        for value in extracted:
            try:
                numeric_values.append(float(value))
            except (TypeError, ValueError):
                errors.append(f"_digits_of returned a nonnumeric token: {value!r}")

        for expected in (-3.5, 12.0, 0.25):
            if not any(abs(value - expected) < 1e-9 for value in numeric_values):
                errors.append(
                    f"_digits_of failed to extract {expected!r}; got {extracted!r}"
                )
    except Exception as exc:
        errors.append(f"_safe_eval/_digits_of test raised unexpectedly: {exc}")

    print("  done")

def test_rubric_items_to_text_preserves_grading_content(errors):
    print("Testing rubric_items_to_text serialization...")
    import grader

    # Include the canonical fields plus compatible aliases used by older item
    # shapes. All aliases intentionally carry the same sentinel values.
    items = [
        {
            "q_label": "Q-SERIAL-1",
            "label": "Q-SERIAL-1",
            "question_label": "Q-SERIAL-1",
            "question_text": "QUESTION_SENTINEL_ALPHA",
            "question": "QUESTION_SENTINEL_ALPHA",
            "solution": "SOLUTION_SENTINEL_ALPHA",
            "reference_solution": "SOLUTION_SENTINEL_ALPHA",
            "max_score": 7,
            "rubric": [
                {
                    "criterion": "CRITERION_SENTINEL_ALPHA",
                    "description": "CRITERION_SENTINEL_ALPHA",
                    "points": 3,
                }
            ],
            "criteria": [
                {
                    "criterion": "CRITERION_SENTINEL_ALPHA",
                    "description": "CRITERION_SENTINEL_ALPHA",
                    "points": 3,
                }
            ],
            "rubric_criteria": [
                {
                    "criterion": "CRITERION_SENTINEL_ALPHA",
                    "description": "CRITERION_SENTINEL_ALPHA",
                    "points": 3,
                }
            ],
        },
        {
            "q_label": "Q-SERIAL-2",
            "label": "Q-SERIAL-2",
            "question_label": "Q-SERIAL-2",
            "question_text": "QUESTION_SENTINEL_BETA",
            "question": "QUESTION_SENTINEL_BETA",
            "solution": "SOLUTION_SENTINEL_BETA",
            "reference_solution": "SOLUTION_SENTINEL_BETA",
            "max_score": 5,
            "rubric": [
                {
                    "criterion": "CRITERION_SENTINEL_BETA",
                    "description": "CRITERION_SENTINEL_BETA",
                    "points": 2,
                }
            ],
            "criteria": [
                {
                    "criterion": "CRITERION_SENTINEL_BETA",
                    "description": "CRITERION_SENTINEL_BETA",
                    "points": 2,
                }
            ],
            "rubric_criteria": [
                {
                    "criterion": "CRITERION_SENTINEL_BETA",
                    "description": "CRITERION_SENTINEL_BETA",
                    "points": 2,
                }
            ],
        },
    ]

    try:
        text = grader.rubric_items_to_text(items)

        if not isinstance(text, str):
            errors.append(
                f"rubric_items_to_text returned {type(text).__name__}, expected str"
            )
        else:
            required = [
                "Q-SERIAL-1",
                "QUESTION_SENTINEL_ALPHA",
                "SOLUTION_SENTINEL_ALPHA",
                "CRITERION_SENTINEL_ALPHA",
                "Q-SERIAL-2",
                "QUESTION_SENTINEL_BETA",
                "SOLUTION_SENTINEL_BETA",
                "CRITERION_SENTINEL_BETA",
            ]
            for sentinel in required:
                if sentinel not in text:
                    errors.append(
                        f"rubric_items_to_text omitted required content: {sentinel}"
                    )

            if "3" not in text:
                errors.append(
                    "rubric_items_to_text omitted the 3-point criterion value"
                )
            if "2" not in text:
                errors.append(
                    "rubric_items_to_text omitted the 2-point criterion value"
                )

            alpha_pos = text.find("QUESTION_SENTINEL_ALPHA")
            beta_pos = text.find("QUESTION_SENTINEL_BETA")
            if alpha_pos == -1 or beta_pos == -1 or alpha_pos >= beta_pos:
                errors.append(
                    "rubric_items_to_text did not preserve question ordering"
                )
    except Exception as exc:
        errors.append(
            f"rubric_items_to_text serialization test raised unexpectedly: {exc}"
        )

    print("  done")



def test_safe_eval_dos_guard(errors):
    """_safe_eval must not hang on huge exponents (claude-opus-5-5, 2026-10-07)."""
    import grader, time
    print("Testing _safe_eval DoS guard...")
    # Huge exponent should return None quickly, not hang.
    start = time.time()
    r = grader._safe_eval("9**9**9")
    elapsed = time.time() - start
    if elapsed > 5:
        errors.append(f"_safe_eval took {elapsed:.1f}s on 9**9**9 (DoS)")
    if r is not None:
        errors.append(f"_safe_eval(9**9**9) should be None, got {r}")
    # String multiplication bomb
    r2 = grader._safe_eval('"a"*10**10')
    if r2 is not None:
        errors.append(f"_safe_eval string bomb should be None, got {r2}")
    # Normal pow still works
    r3 = grader._safe_eval("2**10")
    if r3 != 1024.0:
        errors.append(f"_safe_eval(2**10) should be 1024, got {r3}")
    print("  done")


def test_latex_n_preserves_commands(errors):
    """\\n -> newline must not break \\neq, \\nabla etc. (claude-opus-5-5)."""
    import grader
    print("Testing \\n command preservation...")
    out = grader.normalize_latex("$a \\neq b$")
    if "\\neq" not in out:
        errors.append(f"\\neq was destroyed: {out!r}")
    out2 = grader.normalize_latex("line1\\nline2")
    if out2 != "line1\nline2":
        errors.append(f"literal \\n not converted: {out2!r}")
    print("  done")


def test_balance_braces_double_backslash(errors):
    """\\\\{ is line break + real brace, not escaped (claude-opus-5-5)."""
    import grader
    print("Testing \\\\{ handling...")
    out = grader._balance_braces("a \\\\{b}")
    if out != "a \\\\{b}":
        errors.append(f"\\{{ mishandled: got {out!r}")
    print("  done")


def test_parse_json_loose_multiple_fences(errors):
    """Must try all fences, not just first (claude-opus-5-5)."""
    import grader
    print("Testing multiple fence handling...")
    text = '```python\nx=1\n```\n```json\n{"a": 1}\n```'
    out = grader._parse_json_loose(text)
    if out != {"a": 1}:
        errors.append(f"wrong fence picked: {out!r}")
    print("  done")


def test_rubric_jobs_db_backed(errors):
    """Jobs must be DB-backed, not in-memory (Render multi-worker bug 2026-10-07).

    In-memory _rubric_jobs dict broke under gunicorn --workers 2: job created
    in worker A, poll hit worker B -> 'Job not found'. Uses a temp DB.
    """
    import tempfile, time
    import db as dbmod
    print("Testing DB-backed rubric jobs...")
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    orig = dbmod.DB_PATH
    dbmod.DB_PATH = tmp.name
    try:
        dbmod.init_db()
        # Create -> visible to a "different worker" (fresh connection).
        dbmod.create_rubric_job("job123")
        job = dbmod.get_rubric_job("job123")
        if not job or job["status"] != "running":
            errors.append("job not retrievable after create")
        # Incremental updates.
        dbmod.set_rubric_job_total("job123", 3)
        dbmod.append_rubric_job_item("job123", {"q": "Q1", "idx": 0})
        dbmod.append_rubric_job_item("job123", {"q": "Q2", "idx": 1})
        job = dbmod.get_rubric_job("job123")
        if job["total"] != 3 or len(job["done"]) != 2:
            errors.append(f"incremental updates lost: {job}")
        # Finish.
        dbmod.finish_rubric_job("job123", [{"q": "Q1"}, {"q": "Q2"}, {"q": "Q3"}])
        job = dbmod.get_rubric_job("job123")
        if job["status"] != "done" or len(job["items"]) != 3:
            errors.append(f"finish failed: {job['status']}")
        # Error path.
        dbmod.create_rubric_job("jobErr")
        dbmod.fail_rubric_job("jobErr", "boom")
        job = dbmod.get_rubric_job("jobErr")
        if job["status"] != "error" or job["error"] != "boom":
            errors.append(f"error path failed: {job}")
        # Unknown job -> None (API returns 404).
        if dbmod.get_rubric_job("nope") is not None:
            errors.append("unknown job should return None")
        # Stale detection: running job with old updated timestamp.
        dbmod.create_rubric_job("jobStale")
        conn = dbmod._connect()
        conn.execute("UPDATE rubric_jobs SET updated = ? WHERE job_id = ?",
                     (time.time() - dbmod.JOB_STALE_SECONDS - 1, "jobStale"))
        conn.commit(); conn.close()
        job = dbmod.get_rubric_job("jobStale")
        if not (job["status"] == "running"
                and time.time() - job["updated"] > dbmod.JOB_STALE_SECONDS):
            errors.append("stale job not detectable")
        # Cleanup removes old jobs.
        dbmod.cleanup_old_rubric_jobs(max_age_seconds=0)
        if dbmod.get_rubric_job("job123") is not None:
            errors.append("cleanup did not remove old jobs")
    finally:
        dbmod.DB_PATH = orig
        os.unlink(tmp.name)
    print("  done")


def test_upgrade_to_ap_removed(errors):
    """The separate 'Upgrade to AP Standard' button/endpoint was removed
    (2026-10-07 user decision): AP format is the default Generate Rubric output."""
    print("Testing Upgrade-to-AP removal...")
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(base, "app.py")) as f:
        app_src = f.read()
    if "/api/upgrade_to_ap" in app_src or "def api_upgrade_to_ap" in app_src:
        errors.append("api/upgrade_to_ap endpoint still present in app.py")
    with open(os.path.join(base, "templates", "assignment.html")) as f:
        tpl = f.read()
    if "upgradeApBtn" in tpl or "Upgrade to AP Standard" in tpl:
        errors.append("Upgrade to AP Standard button still in template")
    with open(os.path.join(base, "static", "app.js")) as f:
        js = f.read()
    if "upgrade_to_ap" in js or "upgradeApBtn" in js:
        errors.append("upgrade_to_ap references still in app.js")
    print("  done")


def test_upload_path_traversal_blocked(errors):
    """GET /uploads/../../ai_grader.db must not leak files outside uploads/."""
    print("Testing upload path traversal guard...")
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(base, "app.py")) as f:
        src = f.read()
    # The uploaded_file route must canonicalize and jail to UPLOAD_DIR.
    if "realpath" not in src or "UPLOAD_DIR" not in src:
        errors.append("uploaded_file missing path canonicalization")
    # Simulate the guard logic directly.
    upload_dir = os.path.join(base, "uploads")
    for evil in ["../../ai_grader.db", "..%2f..%2fapp.py", "subdir/../../db.py"]:
        safe = os.path.realpath(os.path.join(upload_dir, evil))
        if safe.startswith(os.path.realpath(upload_dir) + os.sep):
            errors.append(f"traversal not blocked: {evil} -> {safe}")
    print("  done")


def main():
    quick = "--quick" in sys.argv
    errors = []

    # Always run the no-API guardrail tests.
    test_validation_guardrails(errors)
    test_unicode_sanitization(errors)
    test_duplicate_assignment_rejected(errors)
    test_is_ap_format(errors)
    # gpt-5.6-sol generated tests (no API calls)
    test_normalize_latex_environments(errors)
    test_normalize_latex_multiple_environments(errors)
    test_normalize_latex_external_line_separators(errors)
    test_balance_braces_edge_cases(errors)
    test_normalize_latex_brace_repair(errors)
    test_normalize_latex_idempotency(errors)
    test_normalize_latex_misc_edge_cases(errors)
    test_step_number_word_boundaries(errors)
    test_part_step_regex_variants(errors)
    test_solve_rubric_route_exists(errors)
    # Gap analysis tests (gpt-5.6-sol, no API calls, mocked)
    test_solve_with_retries_exception_then_success(errors)
    test_solve_with_retries_stops_on_identical_solution(errors)
    test_build_unified_rubric_routes_teacher_and_ai_items(errors)
    test_build_unified_rubric_ai_fallback_and_default_score(errors)
    test_parse_json_loose_variants(errors)
    test_verify_solution_python_check_confirmed_and_mismatch(errors)
    test_safe_eval_and_digits_of_numeric_edge_cases(errors)
    test_rubric_items_to_text_preserves_grading_content(errors)
    # claude-opus-5-5 review fixes (2026-10-07)
    test_safe_eval_dos_guard(errors)
    test_latex_n_preserves_commands(errors)
    test_balance_braces_double_backslash(errors)
    test_parse_json_loose_multiple_fences(errors)
    # Render deployment fixes (2026-10-07)
    test_rubric_jobs_db_backed(errors)
    test_upgrade_to_ap_removed(errors)
    test_upload_path_traversal_blocked(errors)
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
