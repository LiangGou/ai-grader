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

console.log(failures === 0 ? "\nAll frontend tests passed!" : `\n${failures} FAILURE(S)`);
process.exit(failures === 0 ? 0 : 1);
