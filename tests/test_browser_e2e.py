#!/usr/bin/env python3
"""
Automated browser E2E test for AI Grader.

Full flow:
1. Login
2. Create assignment (or use existing)
3. Upload reference images
4. Generate rubric (with retry on failure)
5. Save rubric
6. Upload student paper images
7. Grade
8. Save grade
9. Verify in Results

Usage:
    python tests/test_browser_e2e.py --assignment-id 2 --ref-image eval/quiz4/quiz4_p1.jpg

Requires: browser task infrastructure (spawn via Muse)
This script generates the browser task instructions.
"""

import argparse
import sys

LOGIN_USER = "teacher"
# Password is retrieved from Render dashboard, not hardcoded
LOGIN_URL = "https://ai-grader-shwa.onrender.com/login"
BASE_URL = "https://ai-grader-shwa.onrender.com"


def build_task(assignment_id, ref_image, student_image, student_name):
    """Build the browser task instructions for full E2E test."""
    return f"""Complete E2E test of AI Grader production app.

Login:
- URL: {LOGIN_URL}
- Username: {LOGIN_USER}
- Password: (provided separately via secure channel)

PHASE 1: Generate Rubric
1. Go to {BASE_URL}/assignment/{assignment_id}
2. If rubric is empty (0 questions):
   a. Upload reference image via the reference upload section
   b. Click "Generate Rubric" button
   c. Wait up to 180 seconds for generation
   d. If any question shows "Needs your review" with "Retry with AI" button:
      - Click "Retry with AI" for each such question
      - Wait up to 90 seconds per retry
      - If still failing after 2 retries, note it and continue
   e. Click "Save Rubric" if there's a save button
3. Verify: count the question rows in Step 2. Report the count.

PHASE 2: Test Grading
1. Go to {BASE_URL}/grade/{assignment_id}
2. Upload student image via Photo Library button
3. Enter student name: "{student_name}" (or verify auto-filled)
4. Click "Grade Now", wait up to 90 seconds
5. If grading succeeds, click "Save Grade"
6. Go to {BASE_URL}/results/{assignment_id}
7. Verify "{student_name}" appears in results

REPORT:
- Phase 1: rubric generated? how many questions? any retries needed?
- Phase 2: grading completed? scores? saved? visible in results?
- Overall: PASS/FAIL with details
- Any errors encountered

Do NOT delete anything. Test data with name "{student_name}" is expected and can be cleaned up later."""


def main():
    p = argparse.ArgumentParser(description="Generate browser E2E test instructions")
    p.add_argument("--assignment-id", type=int, default=2, help="Assignment ID to test")
    p.add_argument("--ref-image", default="eval/quiz4/quiz4_p1.jpg", help="Reference image path")
    p.add_argument("--student-image", default="eval/quiz4/quiz4_p1.jpg", help="Student paper image path")
    p.add_argument("--student-name", default="E2E Test", help="Test student name")
    p.add_argument("--print-task", action="store_true", help="Print the browser task instructions")
    args = p.parse_args()

    task = build_task(args.assignment_id, args.ref_image, args.student_image, args.student_name)

    if args.print_task:
        print(task)
    else:
        print("E2E test task generated. Use with browser.spawn_task:")
        print(f"  Assignment: {args.assignment_id}")
        print(f"  Student: {args.student_name}")
        print()
        print("Run with --print-task to see full instructions.")


if __name__ == "__main__":
    main()
