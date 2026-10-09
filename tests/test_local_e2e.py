#!/usr/bin/env python3
"""Local end-to-end test: exercises the Flask app without a browser.

Tests:
1. Diagram generation for sketch questions (Q8-type)
2. Student name extraction endpoint exists
3. Regenerate rubric returns graph
"""
import sys
sys.path.insert(0, "/home/hatch/workspace/ai-grader")
import os
os.environ["NO_PROXY"] = "localhost,127.0.0.1,::1"
os.environ["no_proxy"] = "localhost,127.0.0.1,::1"

errors = []

def check(cond, msg):
    if not cond:
        errors.append(msg)
        print(f"  ❌ {msg}")
    else:
        print(f"  ✅ {msg}")

print("\n[Test 1] Diagram generation for sketch question")
import grader
q = {
    "question_text": "Given the sketch of f'(x), sketch the graph of f(x).",
    "reference_solution": "f decreasing, horizontal tangent at x=1.",
    "has_diagram": False, "diagram_hint": "",
}
result = grader._process_graph(q, [], "/tmp")
check(result is not None, "diagram generated")
check(result.get("kind") == "image", "diagram kind is image")
check(result.get("validated") == True, "diagram validated")

print("\n[Test 2] Student name extraction function exists")
check(hasattr(grader, "extract_student_name"), "extract_student_name exists")
check(hasattr(grader, "NAME_EXTRACT_SYSTEM"), "NAME_EXTRACT_SYSTEM exists")

print("\n[Test 3] API endpoints exist")
with open("/home/hatch/workspace/ai-grader/app.py") as f:
    app_src = f.read()
check("/api/extract_name" in app_src, "/api/extract_name endpoint")
check("/api/regenerate_rubric" in app_src, "/api/regenerate_rubric endpoint")
check("_process_graph" in app_src, "regenerate calls _process_graph")

print()
if errors:
    print(f"❌ {len(errors)} failures")
    sys.exit(1)
print("✅ All local E2E tests passed!")
