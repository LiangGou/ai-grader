#!/usr/bin/env node
/* Regression tests for frontend logic in static/app.js.
 *
 * Run: node tests/test_frontend.js
 *
 * Every UI bug the user reports gets a test case here so it never regresses.
 */
"use strict";
const fs = require("fs");
const path = require("path");

// Extract canRetryQuestion from app.js (it's inside an IIFE, so we pull
// the function source out and eval it in isolation).
const appJs = fs.readFileSync(path.join(__dirname, "..", "static", "app.js"), "utf8");
const m = appJs.match(/function canRetryQuestion\(item, state\) \{[\s\S]*?\n  \}/);
if (!m) {
  console.error("FATAL: canRetryQuestion not found in app.js");
  process.exit(1);
}
const canRetryQuestion = eval("(" + m[0] + ")");

let failures = 0;
function check(name, actual, expected) {
  const ok = actual === expected;
  console.log((ok ? "  ✅ " : "  ❌ ") + name);
  if (!ok) {
    console.log(`     expected ${expected}, got ${actual}`);
    failures++;
  }
}

console.log("canRetryQuestion:");
// The reported bug (2026-10-07): retry button disappeared for a
// teacher_example question in "warn" state (Q7, validation failed).
// It must show for ANY failed question.
check("ai_solved + warn → true",
  canRetryQuestion({ origin: "ai_solved" }, "warn"), true);
check("teacher_example + warn → true (the reported bug)",
  canRetryQuestion({ origin: "teacher_example" }, "warn"), true);
check("legacy no-origin + warn → true",
  canRetryQuestion({}, "warn"), true);
check("ai_solved + ok → false",
  canRetryQuestion({ origin: "ai_solved" }, "ok"), false);
check("teacher_example + ok → false",
  canRetryQuestion({ origin: "teacher_example" }, "ok"), false);

const assignmentGateLine =
  appJs.split(/\r?\n/).find(
    line =>
      line.includes("if (") &&
      line.includes("window.ASSIGNMENT_ID") &&
      line.includes('"itemsRows"')
  ) || "";

check(
  "assignment initializer gate exists",
  assignmentGateLine !== "",
  true
);
check(
  "assignment initializer gate references solveRubricBtn",
  assignmentGateLine.includes('$("solveRubricBtn")'),
  true
);
check(
  "assignment initializer gate does not reference deleted generateRubricBtn",
  assignmentGateLine.includes('$("generateRubricBtn")'),
  false
);
check(
  "assignment initializer gate requires itemsRows",
  assignmentGateLine.includes('$("itemsRows")'),
  true
);
check(
  "assignment initializer gate requires ASSIGNMENT_ID",
  assignmentGateLine.includes('typeof window.ASSIGNMENT_ID !== "undefined"'),
  true
);

{
  const fsForAssignmentTest = require("fs");
  const pathForAssignmentTest = require("path");
  const assignmentHtml = fsForAssignmentTest.readFileSync(
    pathForAssignmentTest.join(
      __dirname,
      "..",
      "templates",
      "assignment.html"
    ),
    "utf8"
  );

  for (const id of ["itemsRows", "solveRubricBtn"]) {
    const idPattern = new RegExp(
      String.raw`\bid\s*=\s*["']${id}["']`
    );
    check(
      `assignment template contains #${id}`,
      idPattern.test(assignmentHtml),
      true
    );
  }
}

const canRetryMatch = appJs.match(
  /function canRetryQuestion\(item, state\) \{[\s\S]*?\n  \}/
);
check(
  "canRetryQuestion standalone function exists",
  Boolean(canRetryMatch),
  true
);

if (canRetryMatch) {
  const canRetryQuestion = eval("(" + canRetryMatch[0] + ")");

  check(
    "warn state permits retry",
    canRetryQuestion({}, "warn"),
    true
  );
  check(
    "error state does not permit retry",
    canRetryQuestion({}, "error"),
    false
  );
  check(
    "success state does not permit retry",
    canRetryQuestion({}, "success"),
    false
  );
  check(
    "pending state does not permit retry",
    canRetryQuestion({}, "pending"),
    false
  );
  check(
    "missing state does not permit retry",
    canRetryQuestion({}, undefined),
    false
  );
}

console.log(failures === 0 ? "\nAll frontend tests passed!" : `\n${failures} FAILURE(S)`);
process.exit(failures === 0 ? 0 : 1);
