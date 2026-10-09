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


// Grading page photo delete (reported 2026-10-07): each preview thumbnail
// must have a delete button that removes it from the selection.
(function testGradePhotoDelete() {
  // renderPreviews builds .example-item > img.thumb + button.example-del
  // with a thin-line SVG x icon (icon('x')) for each file in selectedFiles.
  const hasDeleteInRender =
    appJs.includes('function renderPreviews()') &&
    appJs.includes("del.innerHTML = icon('x'") &&
    appJs.includes('selectedFiles.splice(idx, 1)');
  check("grade preview has delete button", hasDeleteInRender, true);
  // The delete button must be inside the grading-page block (not just the
  // rubric-example block which already had one).
  const gradeBlock = appJs.slice(appJs.indexOf("Grading page"));
  check("grade block has its own delete", gradeBlock.includes("del.innerHTML = icon('x'"), true);
})();


// Regenerate rubric button (reported 2026-10-07): each rubric row must have
// a "Regenerate" button that calls /api/regenerate_rubric.
(function testRegenButton() {
  const hasBtn = appJs.includes('class="btn btn-small regen-rubric"') &&
    appJs.includes('/api/regenerate_rubric');
  check("regen rubric button exists", hasBtn, true);
})();

// Icon style consistency (user request 2026-10-09): all UI icons must be
// thin-line SVGs matching the Take Photo / Photo Library button style
// (stroke-width 1.7, round caps) — no emoji icons.
(function testIconConsistency() {
  const fs = require("fs");
  const path = require("path");
  const files = fs.readdirSync("templates").filter(f => f.endsWith(".html"))
    .map(f => path.join("templates", f));
  files.push("static/app.js");
  // Emoji / symbol chars used as icons (excludes CJK text).
  const iconRe = /[\u{2600}-\u{27BF}\u{2B00}-\u{2BFF}\u{1F300}-\u{1FAFF}]/u;
  let hasEmoji = false;
  for (const f of files) {
    const content = fs.readFileSync(f, "utf8");
    // Skip the "→" arrow in flow text (typographic, not an icon).
    const lines = content.split("\n");
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i].replace(/→/g, "");
      if (iconRe.test(line)) {
        console.log(`  emoji icon found in ${f}:${i + 1}: ${line.trim().slice(0, 80)}`);
        hasEmoji = true;
      }
    }
  }
  check("no emoji icons in templates/app.js", !hasEmoji, true);
  // All inline SVGs must use the reference stroke width.
  const allContent = files.map(f => fs.readFileSync(f, "utf8")).join("\n");
  const widths = [...allContent.matchAll(/stroke-width="([0-9.]+)"/g)].map(m => m[1]);
  const badWidths = widths.filter(w => w !== "1.7");
  check("all SVGs use stroke-width 1.7", badWidths.length === 0 && widths.length > 0, true);
  // The app.js icon() helper must exist for dynamic icons.
  check("app.js icon() helper exists", appJs.includes("function icon(name"), true);
})();

// Bug 2026-10-09 (E2E v4): saveRubric must return a Promise so finishGenerate
// can wait for the save before claiming "auto-saved". The old fire-and-forget
// hid save failures and the rubric was silently lost on reload.
(function () {
  console.log("saveRubric promise:");
  // Extract the saveRubric function source and check it returns the fetch chain.
  const m = appJs.match(/function saveRubric\(\) \{[\s\S]*?\n  \}/);
  check("saveRubric defined", !!m, true);
  if (m) {
    check("saveRubric returns fetch promise", /return fetch\(/.test(m[0]) || /return Promise\.resolve\(\)\.then/.test(m[0]), true);
    check("saveRubric resolves false on failure", /\.catch\(function \(e\)/.test(m[0]) && /return false/.test(m[0]), true);
    check("saveRubric wraps sync throws (H1)", /Promise\.resolve\(\)\.then/.test(m[0]), true);
    check("saveRubric checks HTTP status (H3)", /!r\.ok/.test(m[0]), true);
  }
  // finishGenerate must wait for saveRubric before alerting.
  check("finishGenerate waits for save", appJs.includes("saveRubric().then(function (ok)"), true);
  // finishGenerate must check items BEFORE saving (C1: don't wipe with empty).
  const fgIdx = appJs.indexOf("function finishGenerate");
  const fgSection = appJs.slice(fgIdx, fgIdx + 2000);
  const checkBeforeSave = fgSection.indexOf("if (!items || !items.length)") < fgSection.indexOf("saveRubric().then");
  check("finishGenerate checks empty before save (C1)", checkBeforeSave, true);
})();

// GPT 5.6 design 2026-10-09: unified color theme.
// Primary actions (Create, Generate Rubric) use btn-primary (purple).
// Destructive actions use btn-danger (red). Secondary uses btn (neutral).
(function () {
  console.log("color theme consistency:");
  const css = fs.readFileSync(path.join(__dirname, "..", "static", "style.css"), "utf8");
  // Primary color meets WCAG AA (darker than old #635bff).
  check("primary is #554dde", css.includes("--primary: #554dde"), true);
  // btn-danger class exists with red tokens.
  check("btn-danger defined", css.includes(".btn-danger"), true);
  check("danger is #b42318", css.includes("--danger: #b42318"), true);
  // btn-small keeps 44px touch target.
  const smallMatch = css.match(/\.btn-small\s*\{[^}]*min-height:\s*44px/);
  check("btn-small min-height 44px", !!smallMatch, true);
  // Templates: Create buttons use btn-primary.
  const indexHtml = fs.readFileSync(path.join(__dirname, "..", "templates", "index.html"), "utf8");
  check("Create uses btn-primary", indexHtml.includes('class="btn btn-primary">Create<'), true);
  check("Create & Set Up Rubric uses btn-primary", indexHtml.includes('class="btn btn-primary">Create &amp; Set Up Rubric<'), true);
  // Delete uses btn-danger.
  check("delete uses btn-danger", indexHtml.includes("btn-danger"), true);
})();

console.log(failures === 0 ? "\nAll frontend tests passed!" : `\n${failures} FAILURE(S)`);
process.exit(failures === 0 ? 0 : 1);
