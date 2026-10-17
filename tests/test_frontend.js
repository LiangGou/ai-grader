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

// Registration anti-double-submit (2026-10-13): the register form disables
// its submit button once a submit starts, so on a slow cold start a retry
// click cannot produce a misleading "username is taken" error after the
// first attempt already created the account.
(function () {
  console.log("register anti-double-submit:");
  const regHtml = fs.readFileSync(path.join(__dirname, "..", "templates", "register.html"), "utf8");
  const m = regHtml.match(/function disableSubmitOnSubmit\(form, btn\) \{[\s\S]*?\n\}/);
  check("disableSubmitOnSubmit exists in register.html", !!m, true);
  if (m) {
    const disableSubmitOnSubmit = eval("(" + m[0] + ")");
    const btn = { disabled: false, textContent: "Create account" };
    let submitHandler = null;
    let pageshowHandler = null;
    const form = {
      addEventListener: function (ev, fn) { if (ev === "submit") submitHandler = fn; },
    };
    const hadWindow = "window" in global;
    const prevWindow = global.window;
    global.window = {
      addEventListener: function (ev, fn) { if (ev === "pageshow") pageshowHandler = fn; },
    };
    disableSubmitOnSubmit(form, btn);
    check("submit handler registered", typeof submitHandler, "function");
    check("button enabled before submit", btn.disabled, false);
    submitHandler();
    check("button disabled on submit", btn.disabled, true);
    check("button label shows progress", btn.textContent, "Creating account...");
    // Back/forward cache can restore the page with the button still
    // disabled; pageshow must restore it or the teacher is stuck.
    check("pageshow handler registered", typeof pageshowHandler, "function");
    pageshowHandler();
    check("button re-enabled on pageshow (bfcache)", btn.disabled, false);
    check("button label restored on pageshow", btn.textContent, "Create account");
    if (hadWindow) global.window = prevWindow; else delete global.window;
    // Production wiring: the real element ids and the invocation itself.
    check("registration form has regForm id", regHtml.includes('<form method="post" id="regForm">'), true);
    check("registration button has submitBtn id", regHtml.includes('<button type="submit" id="submitBtn">'), true);
    check("handler wired to production elements",
      regHtml.includes('disableSubmitOnSubmit(document.getElementById("regForm"), document.getElementById("submitBtn"));'), true);
  }
})();

// Bug (phone report, 2026-10-17): edit a rubric and save, then reopen and
// the old text comes back. Root cause: edits only auto-save via a 1500ms
// debounce, and nothing flushed a pending save when the page was hidden
// or left, so an edit followed quickly by navigating away / closing /
// backgrounding the tab on the phone was lost entirely.
(function () {
  console.log("rubric auto-save flush on page leave:");
  check("flushRubricSave defined", /(?:^|\n)[ \t]*function\s+flushRubricSave\s*\(\s*\)/.test(appJs), true);
  check("pagehide flushes pending save", /window\.addEventListener\(\s*["']pagehide["']\s*,\s*flushRubricSave\s*\)/.test(appJs), true);
  check("visibility hidden flushes pending save", /if\s*\(\s*document\.hidden\s*\)\s*flushRubricSave\(\s*\)/.test(appJs), true);
  check("save fetch uses keepalive", /keepalive\s*:\s*true/.test(appJs), true);
  check("flush is a no-op with no pending save", /if\s*\(\s*saveTimer\s*===\s*null\s*\)\s*return;/.test(appJs), true);
  check("debounced save clears its pending marker", /saveTimer\s*=\s*setTimeout\(\s*function\s*\(\s*\)\s*\{\s*saveTimer\s*=\s*null;\s*saveRubric\(\s*\);\s*\},\s*1500\s*\)/.test(appJs), true);
  // The flush guard compares against null, so the declaration itself
  // must initialise saveTimer to null (not leave it undefined).
  check("saveTimer initialised to null", /var\s+saveTimer\s*=\s*null\s*;/.test(appJs), true);
  // Oversized rubric bodies get a diagnostic (browsers cap keepalive
  // bodies at 64 KiB in bytes; past that the unload save can be dropped).
  check("oversized save body logs a warning", /TextEncoder\(\)\.encode\(body\)\.length\s*>\s*61440/.test(appJs), true);
})();

// Bug (phone report, 2026-10-17) — BEHAVIORAL regression test.
// The pins above only prove the flush code exists. This test executes the
// real autosave block from app.js (saveRubric / scheduleAutoSave /
// flushRubricSave plus the pagehide / visibilitychange registrations) in a
// vm sandbox with stubbed fetch, timers and listeners, and drives the
// exact reported scenario: edit -> page hidden inside the 1500ms debounce
// window -> the pending save must go out (with keepalive) instead of
// being lost. On the pre-fix app.js there is no flush wiring at all, so
// the "save sent on pagehide / on hide" checks below fail — that failure
// is the bug, reproduced. Note: the sandbox asserts the keepalive flag
// is passed to fetch; whether a given browser actually delivers a
// keepalive request during unload is UA behavior a Node test cannot
// prove.
(function () {
  console.log("rubric auto-save flush behavior:");
  const vm = require("vm");
  // Precondition for scenario E/E2: the oversized-body diagnostic needs
  // TextEncoder (makeEnv injects it into the sandbox). Assert it here so
  // a missing stub fails with the real cause, not a warning-count miss.
  check("sandbox precondition: TextEncoder available", typeof TextEncoder, "function");

  // Tiny synchronous Promise so the eval'd code settles in step with the
  // fake timers (saveRubric chains off Promise.resolve().then(...)).
  function settled(state, value) {
    const p = {
      then: function (onF, onR) {
        try {
          if (state === "fulfilled") {
            return typeof onF === "function" ? unwrap(onF(value)) : p;
          }
          return typeof onR === "function" ? unwrap(onR(value)) : p;
        } catch (e) {
          return settled("rejected", e);
        }
      },
      catch: function (onR) { return p.then(undefined, onR); },
    };
    return p;
  }
  function unwrap(r) {
    // Only SyncPromise thenables settle synchronously. If eval'd code
    // ever returns a real Promise, it surfaces here as a rejection —
    // keep every dependency of the eval'd block stubbed (as makeEnv does).
    if (r && typeof r.then === "function") {
      let out = settled("rejected", new Error("async thenable in sync sandbox"));
      r.then(
        function (v) { out = settled("fulfilled", v); },
        function (e) { out = settled("rejected", e); }
      );
      return out;
    }
    return settled("fulfilled", r);
  }
  const SyncPromise = {
    resolve: function (v) { return unwrap(v); },
    reject: function (e) { return settled("rejected", e); },
  };

  // Extract a top-level `function name(...) { ... }` by brace matching
  // (string/comment aware, tolerant of whitespace style). The
  // declaration is located with a line-anchored regex so a comment
  // mentioning the function cannot be matched instead. Null if absent.
  function extractFn(src, name) {
    const decl = src.match(
      new RegExp("(?:^|\\n)[ \\t]*function\\s+" + name + "\\s*\\("));
    if (!decl) return null;
    const start = decl.index + (decl[0].startsWith("\n") ? 1 : 0);
    let i = src.indexOf("{", start);
    if (i < 0) return null;
    let depth = 0;
    let quote = null, lineComment = false, blockComment = false;
    for (let j = i; j < src.length; j++) {
      const ch = src[j], next = src[j + 1];
      if (lineComment) { if (ch === "\n") lineComment = false; continue; }
      if (blockComment) {
        if (ch === "*" && next === "/") { blockComment = false; j++; }
        continue;
      }
      if (quote) {
        if (ch === "\\") { j++; continue; }
        if (ch === quote) quote = null;
        continue;
      }
      if (ch === "/" && next === "/") { lineComment = true; continue; }
      if (ch === "/" && next === "*") { blockComment = true; continue; }
      if (ch === "'" || ch === '"' || ch === "`") { quote = ch; continue; }
      if (ch === "{") depth++;
      else if (ch === "}") {
        depth--;
        if (depth === 0) return src.slice(start, j + 1);
      }
    }
    return null;
  }

  // Extract the addEventListener registration statement(s) for an event
  // that wire up flushRubricSave (paren matched). app.js has other
  // listeners for the same events (e.g. the rubric polling
  // visibilitychange at the bottom) whose handlers reference unrelated
  // IIFE state, so only statements mentioning flushRubricSave qualify.
  // Returns "" when absent (the pre-fix file has none).
  function extractFlushListeners(src, target, eventName) {
    const needle = new RegExp(
      target + "\\.addEventListener\\(\\s*[\"']" + eventName + "[\"']", "g");
    let out = "";
    let match;
    while ((match = needle.exec(src)) !== null) {
      const start = match.index;
      let depth = 0, quote = null, end = -1;
      let lineComment = false, blockComment = false;
      for (let j = src.indexOf("(", start); j < src.length; j++) {
        const ch = src[j], next = src[j + 1];
        if (lineComment) { if (ch === "\n") lineComment = false; continue; }
        if (blockComment) {
          if (ch === "*" && next === "/") { blockComment = false; j++; }
          continue;
        }
        if (quote) {
          if (ch === "\\") { j++; continue; }
          if (ch === quote) quote = null;
          continue;
        }
        if (ch === "/" && next === "/") { lineComment = true; continue; }
        if (ch === "/" && next === "*") { blockComment = true; continue; }
        if (ch === "'" || ch === '"' || ch === "`") { quote = ch; continue; }
        if (ch === "(") depth++;
        else if (ch === ")") {
          depth--;
          if (depth === 0) { end = j; break; }
        }
      }
      if (end < 0) break;
      const stmt = src.slice(start, end + 1) + ";";
      if (stmt.includes("flushRubricSave")) out += stmt + "\n";
      needle.lastIndex = end + 1; // continue past this whole statement
    }
    return out;
  }

  // Fresh sandbox per scenario, mirroring a fresh page load: fresh
  // saveTimer, fresh listener registries, a virtual timer clock.
  function makeEnv(items) {
    const fetchCalls = [];
    const warnings = [];
    const winListeners = {};
    const docListeners = {};
    let now = 0, nextTimerId = 1;
    const timers = new Map();
    const sandbox = {
      Promise: SyncPromise,
      TextEncoder: TextEncoder,
      console: {
        warn: function (msg) { warnings.push(String(msg)); },
        log: function () {},
      },
      fetch: function (url, opts) {
        fetchCalls.push({ url: url, opts: opts || {} });
        return settled("fulfilled", {
          ok: true,
          status: 200,
          json: function () { return settled("fulfilled", { ok: true }); },
        });
      },
      collectItems: function () { return items; },
      setSaveStatus: function () {},
      setTimeout: function (fn, ms) {
        const id = nextTimerId++;
        timers.set(id, { fn: fn, at: now + ms });
        return id;
      },
      clearTimeout: function (id) { timers.delete(id); },
      window: {
        ASSIGNMENT_ID: 4242,
        addEventListener: function (ev, fn) {
          (winListeners[ev] = winListeners[ev] || []).push(fn);
        },
      },
      document: {
        hidden: false,
        addEventListener: function (ev, fn) {
          (docListeners[ev] = docListeners[ev] || []).push(fn);
        },
      },
    };
    vm.createContext(sandbox);
    // Use the saveTimer declaration exactly as written in app.js, so the
    // sandbox exercises the real initial value: with a bare
    // `var saveTimer;` the flush guard (=== null) misfires and scenario
    // C fails behaviorally, not just on the pin. Fail loudly if the
    // declaration cannot be found at all — never silently substitute.
    const decl = appJs.match(/(?:^|\n)[ \t]*var\s+saveTimer(?:\s*=\s*[^;\n]+)?;/);
    if (!decl) throw new Error("saveTimer declaration not found in app.js");
    let boot = decl[0] + "\n";
    const fnNames = ["saveRubric", "scheduleAutoSave", "flushRubricSave"];
    for (const name of fnNames) {
      const fnSrc = extractFn(appJs, name);
      if (fnSrc) boot += fnSrc + "\n";
    }
    boot += extractFlushListeners(appJs, "window", "pagehide");
    boot += extractFlushListeners(appJs, "document", "visibilitychange");
    vm.runInContext(boot, sandbox);
    return {
      fetchCalls: fetchCalls,
      warnings: warnings,
      // An edit in the page: the rubric inputs call scheduleAutoSave().
      edit: function () { vm.runInContext("scheduleAutoSave()", sandbox); },
      advance: function (ms) {
        now += ms;
        for (const entry of Array.from(timers)) {
          if (entry[1].at <= now) { timers.delete(entry[0]); entry[1].fn(); }
        }
      },
      pagehide: function () {
        (winListeners["pagehide"] || []).forEach(function (fn) { fn(); });
      },
      visibility: function (hidden) {
        sandbox.document.hidden = hidden;
        (docListeners["visibilitychange"] || []).forEach(function (fn) { fn(); });
      },
    };
  }

  const editedItems = [{ prompt: "Edited criterion", points: 5 }];

  // Scenario A (the reported bug): edit, then leave the page inside the
  // debounce window. The pending save must be flushed by pagehide.
  {
    const env = makeEnv(editedItems);
    env.edit();
    env.advance(500); // still inside the 1500ms debounce
    check("A: no save sent yet inside debounce window", env.fetchCalls.length, 0);
    env.pagehide();
    check("A: pagehide sends the pending save", env.fetchCalls.length, 1);
    if (env.fetchCalls.length === 1) {
      const call = env.fetchCalls[0];
      check("A: save posts to the rubric endpoint",
        call.url, "/assignment/4242/save_rubric");
      check("A: save uses POST", call.opts.method, "POST");
      check("A: save uses keepalive so it survives unload",
        call.opts.keepalive, true);
      check("A: save body carries the edit",
        call.opts.body, JSON.stringify({ items: editedItems }));
    }
    env.advance(5000); // the flushed debounce timer must not re-send
    check("A: no duplicate save after flush", env.fetchCalls.length, 1);
    check("A: normal-size body logs no size warning", env.warnings.length, 0);
  }

  // Scenario B: backgrounding the tab (visibilitychange -> hidden) also
  // flushes; becoming visible again does not.
  {
    const env = makeEnv(editedItems);
    env.edit();
    env.visibility(true);
    check("B: hiding the tab sends the pending save", env.fetchCalls.length, 1);

    const env2 = makeEnv(editedItems);
    env2.edit();
    env2.visibility(false);
    check("B: visibilitychange to visible sends nothing", env2.fetchCalls.length, 0);
  }

  // Scenario C: with nothing pending the flush is a no-op, and the normal
  // debounce path still saves exactly once (no re-send on later pagehide).
  {
    const env = makeEnv(editedItems);
    env.pagehide();
    check("C: pagehide with no pending edit sends nothing", env.fetchCalls.length, 0);
    env.edit();
    env.advance(1500);
    check("C: debounce still saves after 1500ms", env.fetchCalls.length, 1);
    env.pagehide();
    check("C: pagehide after a completed save sends nothing more",
      env.fetchCalls.length, 1);
  }

  // Scenario D: one navigation on mobile typically fires BOTH pagehide
  // and visibilitychange-hidden for the same edit; the pair must still
  // produce exactly one save, in either order.
  {
    const env = makeEnv(editedItems);
    env.edit();
    env.pagehide();
    env.visibility(true);
    check("D: pagehide + visibility-hidden send exactly one save",
      env.fetchCalls.length, 1);

    const env2 = makeEnv(editedItems);
    env2.edit();
    env2.visibility(true);
    env2.pagehide();
    check("D: visibility-hidden + pagehide send exactly one save",
      env2.fetchCalls.length, 1);
  }

  // Scenario E: an oversized rubric body (over the 64 KiB keepalive cap,
  // counted in BYTES) still gets its save attempt, and the size risk is
  // logged as a diagnostic. E2 uses multibyte chars whose byte length
  // exceeds the cap while their char length does not — the warning must
  // be driven by bytes, not String.length.
  {
    const bigItems = [{ prompt: "x".repeat(70000), points: 5 }];
    const env = makeEnv(bigItems);
    env.edit();
    env.pagehide();
    check("E: oversized body still sends the save", env.fetchCalls.length, 1);
    check("E: oversized body logs a size warning", env.warnings.length, 1);

    const mbItems = [{ prompt: "é".repeat(40000), points: 5 }]; // 40k chars, 80k bytes
    const env2 = makeEnv(mbItems);
    env2.edit();
    env2.pagehide();
    check("E2: multibyte body over the byte cap still sends the save",
      env2.fetchCalls.length, 1);
    check("E2: multibyte body over the byte cap logs a warning",
      env2.warnings.length, 1);
  }
})();

console.log(failures === 0 ? "\nAll frontend tests passed!" : `\n${failures} FAILURE(S)`);
process.exit(failures === 0 ? 0 : 1);
