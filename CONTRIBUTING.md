# Contributing to AI Grader

Thanks for your interest! AI Grader is a Flask web app that grades handwritten
homework and exams with OpenAI vision models. This guide covers setup,
development, and the quality gates every change must pass.

## Setup

Requirements: Python 3.11+, Node.js 18+ (for the frontend test suite).

```bash
git clone https://github.com/LiangGou/ai-grader.git
cd ai-grader
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in GRADER_PASSWORD, ADMIN_PASSWORD, OPENAI_API_KEY
./run.sh               # serves http://localhost:5000
```

Passwords: `GRADER_PASSWORD` and `ADMIN_PASSWORD` must each be at least
12 characters and differ from each other. `PUBLIC_PASSWORD` enables an
optional rate-limited public demo account (see README); leave it empty to
disable. Never commit `.env` or any real credential.

## Running tests

Backend (no paid API calls):

```bash
.venv/bin/python tests/test_regression.py --quick
```

Frontend:

```bash
node tests/test_frontend.js
```

The full backend suite (`tests/test_regression.py` without `--quick`) makes
real OpenAI API calls and costs money — only run it deliberately.

## Quality gates (mandatory)

Every change — bug fix or feature — must pass all of these before it ships:

1. **Regression test first.** Every bug gets a behavioral test case that
   reproduces it (mocked, no API calls): backend in
   `tests/test_regression.py`, frontend JS logic in `tests/test_frontend.js`.
   Structural or source-string assertions alone are not sufficient.
2. **Dual-model code review.** Every code change is reviewed by two
   independent models before shipping. Fix all valid findings (each with its
   own regression test), then re-review until both are clean.
3. **Full test suites.** Backend `--quick` and the frontend suite must pass
   with zero new failures. Pre-existing failures must be individually
   confirmed as pre-existing on the clean tree.
4. **English only.** All docs, code comments, test names, CLI text, and
   prompts must be English. Verify with `grep -rP '\p{Han}'` before shipping.

## Project conventions

- iPhone-first UI: verify layouts at phone resolution.
- Never run ad-hoc tests against the production database, and never restart
  the production server casually — a restart kills in-flight grading requests.
- Never use live assignments for ad-hoc tests, and never delete production
  data without explicit approval.
- Paid evaluation runs always need separate confirmation.

## Pull requests

1. Fork the repo and create a feature branch.
2. Add regression tests for your change (see gate 1).
3. Run both test suites; confirm zero new failures.
4. Open a PR describing what changed, why, and how it was tested.

By contributing, you agree your contributions will be licensed under the
MIT License (see `LICENSE`).
