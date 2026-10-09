/* AI Grader frontend: example-based rubric generation, structured rubric, photo grading, results histogram */
(function () {
  "use strict";

  function $(id) { return document.getElementById(id); }

  /* Thin-line SVG icon set — matches the Take Photo / Photo Library button
     style: 24x24 viewBox, fill none, stroke currentColor, 1.7 width, round
     caps. All UI icons must use these (no emoji icons).
     SECURITY: icon() only emits static path data from the ICONS dict.
     NEVER concatenate untrusted text into innerHTML — use setIconText()
     which inserts text via textContent. */
  var ICONS = Object.freeze({
    check: '<polyline points="20 6 9 17 4 12"/>',
    checkCircle: '<path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/>',
    x: '<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>',
    alert: '<path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>',
    pencil: '<path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/>',
    refresh: '<polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>',
    key: '<path d="M21 2l-2 2m-7.61 7.61a5.5 5.5 0 1 1-7.778 7.778 5.5 5.5 0 0 1 7.777-7.777zm0 0L15.5 7.5m0 0l3 3L22 7l-3-3m-3.5 3.5L19 4"/>',
    trash: '<polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>',
    cpu: '<rect x="4" y="4" width="16" height="16" rx="2" ry="2"/><rect x="9" y="9" width="6" height="6"/><line x1="9" y1="1" x2="9" y2="4"/><line x1="15" y1="1" x2="15" y2="4"/><line x1="9" y1="20" x2="9" y2="23"/><line x1="15" y1="20" x2="15" y2="23"/><line x1="20" y1="9" x2="23" y2="9"/><line x1="20" y1="14" x2="23" y2="14"/><line x1="1" y1="9" x2="4" y2="9"/><line x1="1" y1="14" x2="4" y2="14"/>',
    trendingUp: '<polyline points="23 6 13.5 15.5 8.5 10.5 1 18"/><polyline points="17 6 23 6 23 12"/>',
    gear: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/>'
  });
  function icon(name, size) {
    var body = ICONS[name];
    if (!body) throw new Error("icon(): unknown icon '" + name + "'");
    var s = Math.max(1, Math.min(256, parseInt(size, 10) || 16));
    return '<svg class="icon" width="' + s + '" height="' + s + '" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">' + body + '</svg>';
  }
  /* Set an element to [icon][text] safely: the icon is static markup, the
     text is inserted via textContent (XSS-safe for untrusted strings like
     server errors or OCR output). */
  function setIconText(el, name, text, size) {
    el.textContent = "";
    var t = document.createElement("template");
    t.innerHTML = icon(name, size);
    el.appendChild(t.content.firstElementChild);
    el.appendChild(document.createTextNode(" " + (text == null ? "" : String(text))));
  }

  /* Whether the "Retry with AI" button shows for a rubric card.
     Shows for ANY question in warn (needs-review) state, regardless of
     origin (ai_solved, teacher_example, or legacy items without origin).
     Exported for regression testing. */
  function canRetryQuestion(item, state) {
    return state === "warn";
  }
  // Expose for Node-based regression tests.
  if (typeof module !== "undefined" && module.exports) {
    module.exports.canRetryQuestion = canRetryQuestion;
  }

  /* Click-to-enlarge lightbox: every uploaded-image thumbnail and every
     diagram (img.thumb, img.graph-img) opens fullscreen on tap. Uses event
     delegation so dynamically added images work too. */
  (function initLightbox() {
    var lb = document.getElementById("lightbox");
    if (!lb) return;
    var lbImg = lb.querySelector("img");
    function close() { lb.classList.remove("open"); lbImg.removeAttribute("src"); }
    lb.addEventListener("click", close);
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") close();
    });
    document.addEventListener("click", function (e) {
      var t = e.target;
      if (t && t.tagName === "IMG" && (t.classList.contains("thumb") || t.classList.contains("graph-img"))) {
        e.preventDefault();
        lbImg.src = t.currentSrc || t.src;
        lb.classList.add("open");
      }
    });
  })();

  /* Shrink a photo client-side before upload. Phone photos are multi-MB and
     stall on slow connections; downscale to max 1600px as JPEG (~0.82).
     Also converts HEIC etc. to real JPEG. Falls back to the original file. */
  function shrinkImage(file) {
    return new Promise(function (resolve) {
      var done = function (f) { resolve(f); };
      try {
        var url = URL.createObjectURL(file);
        var img = new Image();
        img.onload = function () {
          URL.revokeObjectURL(url);
          try {
            var MAX = 1600;
            var scale = Math.min(1, MAX / Math.max(img.naturalWidth, img.naturalHeight));
            var w = Math.max(1, Math.round(img.naturalWidth * scale));
            var h = Math.max(1, Math.round(img.naturalHeight * scale));
            var c = document.createElement("canvas");
            c.width = w; c.height = h;
            c.getContext("2d").drawImage(img, 0, 0, w, h);
            c.toBlob(function (blob) {
              if (blob) {
                var name = (file.name || "photo").replace(/\.[^.]+$/, "") + ".jpg";
                done(new File([blob], name, { type: "image/jpeg" }));
              } else { done(file); }
            }, "image/jpeg", 0.82);
          } catch (e) { done(file); }
        };
        img.onerror = function () { URL.revokeObjectURL(url); done(file); };
        img.src = url;
      } catch (e) { done(file); }
    });
  }

  /* Render LaTeX ($...$ / $$...$$) inside an element with KaTeX when available. */
  function renderMath(el) {
    if (!el || typeof renderMathInElement !== "function") return;
    try {
      renderMathInElement(el, {
        delimiters: [
          { left: "$$", right: "$$", display: true },
          { left: "$", right: "$", display: false }
        ],
        throwOnError: false
      });
    } catch (e) { /* leave raw text on failure */ }
  }

  /* Draw a sampled y=f(x) polyline with axes and grid. */
  function drawFunctionGraph(canvas, g) {
    var ctx = canvas.getContext("2d");
    var W = canvas.width, H = canvas.height, pad = 28;
    var x0 = g.x_min, x1 = g.x_max, y0 = g.y_min, y1 = g.y_max;
    function X(x) { return pad + (x - x0) / (x1 - x0) * (W - 2 * pad); }
    function Y(y) { return H - pad - (y - y0) / (y1 - y0) * (H - 2 * pad); }
    ctx.clearRect(0, 0, W, H);
    ctx.strokeStyle = "#e0e0e0"; ctx.lineWidth = 1;
    for (var i = 0; i <= 4; i++) {
      var gx = X(x0 + (x1 - x0) * i / 4), gy = Y(y0 + (y1 - y0) * i / 4);
      ctx.beginPath(); ctx.moveTo(gx, pad); ctx.lineTo(gx, H - pad); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(pad, gy); ctx.lineTo(W - pad, gy); ctx.stroke();
    }
    // axes
    ctx.strokeStyle = "#999"; ctx.lineWidth = 1.5;
    if (y0 <= 0 && 0 <= y1) { ctx.beginPath(); ctx.moveTo(pad, Y(0)); ctx.lineTo(W - pad, Y(0)); ctx.stroke(); }
    if (x0 <= 0 && 0 <= x1) { ctx.beginPath(); ctx.moveTo(X(0), pad); ctx.lineTo(X(0), H - pad); ctx.stroke(); }
    // curve
    ctx.strokeStyle = "#1565c0"; ctx.lineWidth = 2.5; ctx.beginPath();
    var started = false;
    g.points.forEach(function (p) {
      var px = X(p[0]), py = Y(p[1]);
      if (!started) { ctx.moveTo(px, py); started = true; }
      else ctx.lineTo(px, py);
    });
    ctx.stroke();
    ctx.fillStyle = "#666"; ctx.font = "11px sans-serif";
    ctx.fillText("x", W - pad + 4, Y(0) + 4);
    ctx.fillText("y", X(0) - 12, pad - 6);
  }

  /* ---------- Assignment setup page: digitized rubric ---------- */
  // Auto-save helpers live at IIFE top level so rubricRow() can reference
  // scheduleAutoSave (they were previously block-scoped inside the `if`,
  // which made rubricRow throw "scheduleAutoSave is not defined").
  var saveTimer = null;
  function setSaveStatus(name, text, isError) {
    var el = $("rubricSaveStatus");
    if (!el) return;
    setIconText(el, name, text, 14);
    el.classList.toggle("error", !!isError);
  }
  // Every rubric change auto-saves (debounced) — there is no save button.
  function saveRubric() {
    var items = collectItems(); // may be [] if the teacher deleted all rows
    setSaveStatus("Saving…", false);
    fetch("/assignment/" + window.ASSIGNMENT_ID + "/save_rubric", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items: items })
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data.ok) { setSaveStatus("Save failed", true); return; }
        setSaveStatus("check", "Auto-saved", false);
      })
      .catch(function () { setSaveStatus("Save failed", true); });
  }
  function scheduleAutoSave() {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(saveRubric, 1500);
  }
  function rubricRow(item) {
    item = item || {};
    var state = item.verified === "yes" ? "ok" : (item.verify_note ? "warn" : "manual");
    var row = document.createElement("div");
    row.className = "rubric-card" + (state === "warn" ? " needs-review" : "");
    var badge = state === "ok"
      ? '<span class="verify-badge ok">' + icon('checkCircle', 14) + ' Verified</span>'
      : state === "warn"
        ? '<span class="verify-badge warn">' + icon('alert', 14) + ' Needs your review</span>'
        : '<span class="verify-badge manual">' + icon('pencil', 14) + ' Manual</span>';
    var note = (state === "warn" && item.verify_note)
      ? '<p class="verify-note">' + escapeHtml(item.verify_note) + '</p>' : "";
    // Retry panel: shown for ANY question needing review, regardless of origin.
    // (ai_solved, teacher_example, or legacy items without origin.)
    var canRetry = canRetryQuestion(item, state);
    var retryHtml = "";
    if (canRetry) {
      retryHtml =
        '<div class="retry-box">' +
          '<button type="button" class="btn btn-small retry-toggle">' + icon('refresh', 14) + ' Retry with AI</button>' +
          '<div class="retry-panel" style="display:none">' +
            '<label>Model: <select class="retry-model">' +
              '<option value="o3">o3 (default)</option>' +
              '<option value="o3-high">o3 high effort (slower, more thorough)</option>' +
              '<option value="gpt-4o">gpt-4o (different approach)</option>' +
            '</select></label> ' +
            '<button type="button" class="btn btn-small btn-primary retry-go">Retry</button>' +
            '<div class="retry-status muted"></div>' +
            '<details class="retry-debug"><summary>Debug: raw model output</summary><pre class="retry-raw"></pre></details>' +
          '</div>' +
        '</div>';
    }
    row.innerHTML =
      '<div class="rubric-top">' +
        '<input class="q" type="text" placeholder="Question" value="' + escapeHtml(item.q) + '">' +
        '<input class="ms" type="number" step="0.5" min="0" placeholder="Max pts" value="' + escapeHtml(item.max_score) + '">' +
        badge +
        '<button type="button" class="btn btn-small row-del">' + icon('x', 14) + '</button>' +
      '</div>' +
      '<label class="rubric-field"><span>Question (digitized)</span>' +
        '<textarea class="qt" rows="2" placeholder="Question text as read from the example">' + escapeHtml(item.question_text) + '</textarea>' +
        '<div class="math-preview"></div></label>' +
      '<label class="rubric-field"><span>Reference solution (digitized)</span>' +
        '<textarea class="rs" rows="3" placeholder="Reference solution as read from the example">' + escapeHtml(item.reference_solution) + '</textarea>' +
        '<div class="math-preview"></div></label>' +
      '<label class="rubric-field"><span>Scoring / deduction rules ' +
        '<button type="button" class="btn btn-small regen-rubric" title="Regenerate rubric from the current solution using AP standards">' + icon('refresh', 14) + ' Regenerate</button>' +
        '<span class="regen-status muted"></span></span>' +
        '<textarea class="cr" rows="3" placeholder="Scoring/deduction rules">' + escapeHtml(item.criteria) + '</textarea></label>' +
      '<div class="graph-slot"></div>' +
      note +
      retryHtml;
    row._graph = item.graph || null;
    row.querySelector(".row-del").addEventListener("click", function () { row.remove(); scheduleAutoSave(); });
    // Regenerate rubric from current solution, AP standards.
    row.querySelector(".regen-rubric").addEventListener("click", function () {
      var btn = this;
      var status = row.querySelector(".regen-status");
      btn.disabled = true;
      status.textContent = " ⏳ Regenerating…";
      fetch("/api/regenerate_rubric", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          assignment_id: window.ASSIGNMENT_ID,
          q: row.querySelector(".q").value,
          question_text: row.querySelector(".qt").value,
          reference_solution: row.querySelector(".rs").value,
          max_score: parseFloat(row.querySelector(".ms").value) || 0
        })
      })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          btn.disabled = false;
          if (!data.ok) {
            setIconText(status, 'x', data.error || "failed", 14);
            return;
          }
          row.querySelector(".cr").value = data.criteria || "";
          // Update graph if the backend generated one
          if (data.graph && data.graph.kind === "image" && data.graph.url) {
            row._graph = data.graph;
            var slot = row.querySelector(".graph-slot");
            slot.innerHTML = "";
            var cap = document.createElement("p");
            cap.className = "graph-cap";
            setIconText(cap, 'cpu', "AI-generated diagram", 14);
            var im = document.createElement("img");
            im.className = "graph-img";
            im.src = data.graph.url;
            im.alt = "AI-generated diagram";
            slot.appendChild(cap);
            slot.appendChild(im);
          }
          setIconText(status, 'check', "Done", 14);
          scheduleAutoSave();
          setTimeout(function () { status.textContent = ""; }, 3000);
        })
        .catch(function (e) {
          btn.disabled = false;
          setIconText(status, 'x', e.message, 14);
        });
    });
    // Retry wiring.
    var retryToggle = row.querySelector(".retry-toggle");
    if (retryToggle) {
      retryToggle.addEventListener("click", function () {
        var panel = row.querySelector(".retry-panel");
        panel.style.display = panel.style.display === "none" ? "" : "none";
      });
      row.querySelector(".retry-go").addEventListener("click", function () {
        var modelSel = row.querySelector(".retry-model").value;
        var model = modelSel === "gpt-4o" ? "gpt-4o" : "o3";
        var highEffort = modelSel === "o3-high";
        var status = row.querySelector(".retry-status");
        var rawPre = row.querySelector(".retry-raw");
        var goBtn = row.querySelector(".retry-go");
        goBtn.disabled = true;
        status.textContent = "⏳ Retrying with " + modelSel + "… (about 30-60s)";
        rawPre.textContent = "";
        fetch("/api/retry_question", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            assignment_id: window.ASSIGNMENT_ID,
            q: row.querySelector(".q").value,
            question_text: row.querySelector(".qt").value,
            max_score: parseFloat(row.querySelector(".ms").value) || 0,
            model: model,
            high_effort: highEffort
          })
        })
          .then(function (r) {
            if (!r.ok) throw new Error("Server returned " + r.status + ". Please try again.");
            return r.json();
          })
          .then(function (data) {
            goBtn.disabled = false;
            if (!data.ok) {
              setIconText(status, 'x', data.error || "retry failed", 14);
              return;
            }
            var res = data.result;
            // Show debug output.
            rawPre.textContent = (res.debug || []).map(function (d) {
              return "=== " + d.step + " (" + d.model + (d.effort ? ", effort=" + d.effort : "") + ") ===\n" + d.raw;
            }).join("\n\n");
            row.querySelector(".retry-debug").open = true;
            if (res.verified === "yes") {
              setIconText(status, 'check', "Verified on retry — solution and rubric updated.", 14);
            } else {
              setIconText(status, 'alert', "Still needs review: " + (res.verify_note || ""), 14);
            }
            // Update the card with the new solution.
            row.querySelector(".rs").value = res.solution || "";
            row.querySelector(".rs").dispatchEvent(new Event("input"));
            row.querySelector(".cr").value = res.criteria || "";
            var b = row.querySelector(".verify-badge");
            if (b) {
              b.className = "verify-badge " + (res.verified === "yes" ? "ok" : "warn");
              setIconText(b, res.verified === "yes" ? 'check' : 'alert', res.verified === "yes" ? "Verified" : "Needs your review", 14);
            }
            var vn = row.querySelector(".verify-note");
            if (vn) vn.textContent = res.verify_note || "";
            scheduleAutoSave();
          })
          .catch(function (e) {
            goBtn.disabled = false;
            setIconText(status, 'x', "Request failed: " + e.message, 14);
          });
      });
    }
    // Live KaTeX previews under the digitized inputs.
    ["qt", "rs"].forEach(function (cls) {
      var inp = row.querySelector("." + cls);
      var prev = inp.parentElement.querySelector(".math-preview");
      var update = function () {
        if (!inp.value.trim()) { prev.innerHTML = ""; prev.style.display = "none"; return; }
        prev.style.display = "";
        prev.textContent = inp.value;
        renderMath(prev);
      };
      inp.addEventListener("input", update);
      update();
    });
    // Graph: regenerated function plot or cropped diagram image.
    (function () {
      var slot = row.querySelector(".graph-slot");
      var g = row._graph;
      if (!g) return;
      if (g.kind === "function" && g.points && g.points.length) {
        var cap = document.createElement("p");
        cap.className = "graph-cap";
        setIconText(cap, 'trendingUp', "Regenerated graph" + (g.expression ? ": y = " + g.expression : ""), 14);
        var cv = document.createElement("canvas");
        cv.className = "graph-canvas";
        cv.width = 600; cv.height = 360;
        slot.appendChild(cap);
        slot.appendChild(cv);
        drawFunctionGraph(cv, g);
      } else if (g.kind === "image" && g.url) {
        var cap2 = document.createElement("p");
        cap2.className = "graph-cap";
        setIconText(cap2, 'cpu', "AI-generated diagram", 14);
        var im = document.createElement("img");
        im.className = "graph-img";
        im.src = g.url;
        im.alt = "Cropped diagram from the reference example";
        slot.appendChild(cap2);
        slot.appendChild(im);
      }
    })();
    // If the teacher edits a flagged row, treat it as human-verified.
    if (state === "warn") {
      ["qt", "rs"].forEach(function (cls) {
        row.querySelector("." + cls).addEventListener("input", function () {
          var b = row.querySelector(".verify-badge");
          if (b && b.classList.contains("warn")) {
            b.className = "verify-badge ok";
            setIconText(b, 'check', "Verified by you", 14);
            row.classList.remove("needs-review");
            var n = row.querySelector(".verify-note");
            if (n) n.remove();
          }
        });
      });
    }
    // Every keystroke auto-saves (debounced) — no save button needed.
    row.querySelectorAll("input, textarea").forEach(function (inp) {
      inp.addEventListener("input", scheduleAutoSave);
    });
    return row;
  }
  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
  function collectItems() {
    var items = [];
    document.querySelectorAll("#itemsRows .rubric-card").forEach(function (row) {
      var q = row.querySelector(".q").value.trim();
      if (!q) return;
      var badge = row.querySelector(".verify-badge");
      items.push({
        q: q,
        max_score: parseFloat(row.querySelector(".ms").value) || 0,
        criteria: row.querySelector(".cr").value.trim(),
        question_text: row.querySelector(".qt").value.trim(),
        reference_solution: row.querySelector(".rs").value.trim(),
        verified: badge && badge.classList.contains("ok") ? "yes" : "review",
        verify_note: (function () {
          var n = row.querySelector(".verify-note");
          return n ? n.textContent : "";
        })(),
        graph: row._graph || null
      });
    });
    return items;
  }

  if ($("itemsRows") && typeof window.ASSIGNMENT_ID !== "undefined" && $("solveRubricBtn")) {
    var rowsBox = $("itemsRows");
    (window.EXISTING_ITEMS || []).forEach(function (it) { rowsBox.appendChild(rubricRow(it)); });

    var savedExamples = (window.SAVED_EXAMPLES || []).map(function (name) {
      return { name: name, checked: true };
    });
    function renderExampleList() {
      var list = $("exampleList");
      list.innerHTML = "";
      savedExamples.forEach(function (ex) {
        var item = document.createElement("div");
        item.className = "example-item";
        var cb = document.createElement("input");
        cb.type = "checkbox";
        cb.checked = ex.checked;
        cb.title = "Include in rubric generation";
        cb.addEventListener("change", function () { ex.checked = cb.checked; });
        var img = document.createElement("img");
        img.className = "thumb";
        img.src = "/uploads/" + encodeURIComponent(ex.name);
        var del = document.createElement("button");
        del.type = "button";
        del.className = "btn btn-small example-del";
        del.innerHTML = icon('x', 14);
        del.title = "Delete this example";
        del.setAttribute("aria-label", "Delete this example");
        del.addEventListener("click", function () { deleteExample(ex.name); });
        item.appendChild(cb);
        item.appendChild(img);
        item.appendChild(del);
        list.appendChild(item);
      });
      $("noExamplesMsg").style.display = savedExamples.length ? "none" : "";
      $("exampleCount").textContent = savedExamples.length ? "(" + savedExamples.length + ")" : "";
    }
    function uploadExamples(fileList) {
      var files = Array.prototype.slice.call(fileList);
      if (!files.length) return;
      $("exampleUploading").style.display = "";
      $("exampleUploading").textContent = "⏳ Preparing…";
      Promise.all(files.map(shrinkImage)).then(function (shrunk) {
        var fd = new FormData();
        shrunk.forEach(function (f) { fd.append("images", f); });
        $("exampleUploading").textContent = "⏳ Uploading…";
        return fetch("/assignment/" + window.ASSIGNMENT_ID + "/upload_rubric_example", { method: "POST", body: fd });
      })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          $("exampleUploading").style.display = "none";
          if (!data.ok) { alert("Upload failed: " + (data.error || "unknown error")); return; }
          var prevChecked = {};
          savedExamples.forEach(function (ex) { prevChecked[ex.name] = ex.checked; });
          savedExamples = (data.examples || []).map(function (name) {
            return { name: name, checked: prevChecked.hasOwnProperty(name) ? prevChecked[name] : true };
          });
          renderExampleList();
        })
        .catch(function (e) {
          $("exampleUploading").style.display = "none";
          alert("Upload failed: " + e.message);
        });
    }
    function deleteExample(name) {
      if (!confirm("Delete this saved example?")) return;
      fetch("/assignment/" + window.ASSIGNMENT_ID + "/delete_rubric_example", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filename: name })
      })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (!data.ok) { alert("Delete failed: " + (data.error || "unknown error")); return; }
          var prevChecked = {};
          savedExamples.forEach(function (ex) { prevChecked[ex.name] = ex.checked; });
          savedExamples = (data.examples || []).map(function (n) {
            return { name: n, checked: prevChecked.hasOwnProperty(n) ? prevChecked[n] : true };
          });
          renderExampleList();
        })
        .catch(function (e) { alert("Delete failed: " + e.message); });
    }
    $("examplePhotoInput").addEventListener("change", function () { uploadExamples(this.files); this.value = ""; });
    $("exampleLibraryInput").addEventListener("change", function () { uploadExamples(this.files); this.value = ""; });
    renderExampleList();

    $("addRowBtn").addEventListener("click", function () {
      rowsBox.appendChild(rubricRow());
      scheduleAutoSave();
    });

    var renderedGenIdx = {}; // idx -> true for the in-progress streaming generation

    // Insert a freshly-validated question row in idx order among generated rows.
    function insertGenRow(item) {
      var row = rubricRow(item);
      row.dataset.genIdx = item.idx;
      var kids = rowsBox.children;
      for (var i = 0; i < kids.length; i++) {
        var k = kids[i];
        if (k.dataset && k.dataset.genIdx !== undefined && parseInt(k.dataset.genIdx, 10) > item.idx) {
          rowsBox.insertBefore(row, k);
          return;
        }
      }
      rowsBox.appendChild(row);
    }

    function finishGenerate(btn, items) {
      btn.disabled = false;
      setIconText(btn, 'key', "Generate Rubric", 20);
      // Make sure every item is on screen (in case some arrived with the final poll).
      (items || []).forEach(function (it) {
        if (!renderedGenIdx[it.idx]) { insertGenRow(it); renderedGenIdx[it.idx] = true; }
      });
      saveRubric(); // final auto-save of the complete rubric
      if (!items || !items.length) {
        alert("Could not read any questions from the example. Try clearer photos, or add rows manually.");
      } else {
        var okCount = items.filter(function (it) { return it.verified === "yes"; }).length;
        var needCount = items.length - okCount;
        alert("Digitized " + items.length + " question(s): " + okCount + " verified" +
          (needCount ? ", " + needCount + " need your review" : "") +
          " — auto-saved. Every edit auto-saves too.");
      }
    }

    function failGenerate(btn, msg) {
      btn.disabled = false;
      setIconText(btn, 'key', "Generate Rubric", 20);
      alert(msg);
    }

    // Generation runs as a background job (~1 min). We poll for the result so
    // a slow/flaky connection can't kill it mid-flight.
    var activeRubricPoll = null; // re-poll when the tab becomes visible again
    function pollRubricJob(jobId, btn) {
      var attempts = 0, maxAttempts = 100; // ~5 minutes
      var timer = null;
      function pollOnce() {
        attempts++;
        fetch("/api/rubric_job/" + jobId)
          .then(function (r) { return r.json(); })
          .then(function (data) {
            if (!data.ok) { // job lost, e.g. server restarted
              clearInterval(timer); activeRubricPoll = null;
              failGenerate(btn, "Generation was interrupted (" + (data.error || "job not found") + "). Please tap Generate again.");
              return;
            }
            // Stream newly-validated questions: render each and auto-save.
            var fresh = false;
            (data.done || []).forEach(function (it) {
              if (!renderedGenIdx[it.idx]) {
                insertGenRow(it);
                renderedGenIdx[it.idx] = true;
                fresh = true;
              }
            });
            if (fresh) saveRubric();
            var nDone = Object.keys(renderedGenIdx).length;
            var total = data.total || 0;
            if (data.status === "running") {
              var elapsed = attempts * 3;
              setIconText(btn, 'refresh', total > 0 ? "Validated " + nDone + "/" + total + " questions…" : "Reading questions… (" + elapsed + "s)", 20);
              if (attempts >= maxAttempts) {
                clearInterval(timer); activeRubricPoll = null;
                failGenerate(btn, "Generation is taking longer than expected. Please try again.");
              }
              return;
            }
            clearInterval(timer); activeRubricPoll = null;
            if (data.status === "error") {
              failGenerate(btn, "Rubric generation failed: " + (data.error || "unknown error"));
              return;
            }
            finishGenerate(btn, data.items);
          })
          .catch(function (err) {
            // Never swallow errors silently: a bug here used to cause an
            // infinite "Reading questions…" loop with no visible error.
            if (window.console && console.error) console.error("Rubric poll failed:", err);
            // Transient network blip: keep polling unless we've waited too long.
            if (attempts >= maxAttempts) {
              clearInterval(timer); activeRubricPoll = null;
              failGenerate(btn, "Lost connection to the server. Please try again.");
            }
          });
      }
      timer = setInterval(pollOnce, 3000);
      activeRubricPoll = pollOnce;
      pollOnce(); // check immediately, don't wait 3s for the first poll
    }
    // iOS throttles timers in background tabs: re-check as soon as the user returns.
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden && activeRubricPoll) activeRubricPoll();
    });

    // Unified flow: uses teacher's worked solutions if shown, otherwise AI
    // solves each question itself. Either way builds an AP-style rubric
    // from the printed point values (AP format is the default output).
    $("solveRubricBtn").addEventListener("click", function () {
      var include = savedExamples.filter(function (ex) { return ex.checked; }).map(function (ex) { return ex.name; });
      if (!include.length) { alert("Please take or upload the question photos first, and make sure at least one saved example is checked."); return; }
      var btn = $("solveRubricBtn");
      btn.disabled = true;
      setIconText(btn, 'refresh', "Generating rubric…", 20);
      rowsBox.innerHTML = ""; // fresh list; questions stream in one by one
      renderedGenIdx = {};
      fetch("/api/solve_rubric", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ assignment_id: window.ASSIGNMENT_ID, include: include })
      })
        .then(function (r) {
          if (!r.ok) throw new Error("Server returned " + r.status + ". Please try again.");
          return r.json();
        })
        .then(function (data) {
          if (!data.ok || !data.job_id) {
            failGenerate(btn, "Solve failed: " + (data.error || "unknown error"));
            return;
          }
          pollRubricJob(data.job_id, btn);
        })
        .catch(function (e) {
          failGenerate(btn, "Request failed: " + e.message);
        });
    });

    /* Poll loop, streaming, and generation click handler below use the
       top-level saveRubric / scheduleAutoSave defined above. */
  }

  /* ---------- Grading page ---------- */
  if ($("gradeBtn") && typeof window.ASSIGNMENT_ID !== "undefined") {
    var selectedFiles = [];
    function renderPreviews() {
      var preview = $("preview");
      preview.innerHTML = "";
      selectedFiles.forEach(function (f, idx) {
        var wrap = document.createElement("div");
        wrap.className = "example-item";
        var img = document.createElement("img");
        img.className = "thumb";
        img.src = URL.createObjectURL(f);
        var del = document.createElement("button");
        del.type = "button";
        del.className = "btn btn-small example-del";
        del.innerHTML = icon('x', 14);
        del.title = "Remove this photo";
        del.setAttribute("aria-label", "Remove this photo");
        del.addEventListener("click", function () {
          selectedFiles.splice(idx, 1);
          renderPreviews();
        });
        wrap.appendChild(img);
        wrap.appendChild(del);
        preview.appendChild(wrap);
      });
    }
    function handlePicked(input) {
      var files = Array.prototype.slice.call(input.files);
      input.value = "";  // allow picking the same file again
      if (!files.length) return;
      Promise.all(files.map(shrinkImage)).then(function (shrunk) {
        shrunk.forEach(function (f) { selectedFiles.push(f); });
        renderPreviews();
        // Auto-extract student name from the first photo (user can override).
        // Only if the name field is empty.
        var nameInput = $("studentName");
        if (nameInput && !nameInput.value.trim() && shrunk.length) {
          var fd = new FormData();
          fd.append("image", shrunk[0]);
          fetch("/api/extract_name", { method: "POST", body: fd })
            .then(function (r) { return r.json(); })
            .then(function (data) {
              if (data.ok && data.name && !nameInput.value.trim()) {
                nameInput.value = data.name;
              }
            })
            .catch(function () { /* silent: manual entry still works */ });
        }
      });
    }
    $("photoInput").addEventListener("change", function () { handlePicked(this); });
    $("libraryInput").addEventListener("change", function () { handlePicked(this); });

    var currentResult = null;

    $("gradeBtn").addEventListener("click", function () {
      var name = $("studentName").value.trim();
      // Name is optional: backend auto-extracts from the first photo if empty.
      if (!selectedFiles.length) { alert("Please take/upload paper photos first."); return; }
      var fd = new FormData();
      fd.append("assignment_id", window.ASSIGNMENT_ID);
      fd.append("student_name", name);
      selectedFiles.forEach(function (f) { fd.append("images", f); });
      $("gradeLoading").style.display = "";
      $("gradeError").style.display = "none";
      $("gradeResult").style.display = "none";
      $("gradeBtn").disabled = true;
      fetch("/api/grade", { method: "POST", body: fd })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          $("gradeLoading").style.display = "none";
          $("gradeBtn").disabled = false;
          if (!data.ok) {
            var eb = $("gradeError");
            eb.textContent = "Grading failed: " + data.error;
            eb.style.display = "";
            return;
          }
          currentResult = data.result;
          currentResult.photos = data.photos || [];
          renderResult(name, data.result);
        })
        .catch(function (e) {
          $("gradeLoading").style.display = "none";
          $("gradeBtn").disabled = false;
          var eb = $("gradeError");
          eb.textContent = "Request failed: " + e.message;
          eb.style.display = "";
        });
    });

    function renderResult(name, result) {
      $("resultStudent").textContent = "— " + name;
      var box = $("questionsBox");
      box.innerHTML = "";
      result.questions.forEach(function (q, i) {
        var div = document.createElement("div");
        div.className = "q-block";
        div.innerHTML =
          '<div class="q-head"><strong>' + escapeHtml(q.q) + '</strong>' +
          '<span><input type="number" class="score-input" data-i="' + i + '" step="0.5" min="0" max="' + q.max_score + '" value="' + q.score + '"> / ' + q.max_score + ' pts</span></div>' +
          (q.answer_seen ? '<p class="q-seen">Answer detected: ' + escapeHtml(q.answer_seen) + '</p>' : "") +
          '<p class="q-feedback">' + escapeHtml(q.feedback) + '</p>';
        box.appendChild(div);
      });
      $("totalInput").value = result.total;
      $("totalInput").max = result.max_total;
      $("maxTotal").textContent = result.max_total + " pts";
      $("overallFeedback").value = result.overall_feedback || "";
      $("gradeResult").style.display = "";
      $("saveMsg").textContent = "";
      box.querySelectorAll(".score-input").forEach(function (inp) {
        inp.addEventListener("input", recalcTotal);
      });
    }

    function recalcTotal() {
      var sum = 0;
      document.querySelectorAll("#questionsBox .score-input").forEach(function (inp) {
        sum += parseFloat(inp.value) || 0;
      });
      $("totalInput").value = Math.round(sum * 100) / 100;
    }
    $("totalInput").addEventListener("input", function () { /* total can be manually adjusted */ });

    $("saveGradeBtn").addEventListener("click", function () {
      if (!currentResult) return;
      var questions = currentResult.questions.map(function (q, i) {
        var inp = document.querySelector('#questionsBox .score-input[data-i="' + i + '"]');
        var s = inp ? parseFloat(inp.value) || 0 : q.score;
        s = Math.max(0, Math.min(s, q.max_score));
        return { q: q.q, score: s, max_score: q.max_score, answer_seen: q.answer_seen, feedback: q.feedback };
      });
      var total = parseFloat($("totalInput").value) || 0;
      var btn = $("saveGradeBtn");
      btn.disabled = true;
      fetch("/api/save_grade", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          assignment_id: window.ASSIGNMENT_ID,
          student_name: $("studentName").value.trim(),
          questions: questions,
          total: total,
          max_total: currentResult.max_total,
          overall_feedback: $("overallFeedback").value,
          photos: currentResult.photos || []
        })
      })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          btn.disabled = false;
          setIconText($("saveMsg"), data.ok ? 'check' : 'x', data.ok ? "Saved — you can grade the next student." : "Save failed: " + data.error, 14);
          if (data.ok) { $("studentName").value = ""; selectedFiles = []; renderPreviews(); $("gradeResult").style.display = "none"; currentResult = null; }
        })
        .catch(function (e) {
          btn.disabled = false;
          setIconText($("saveMsg"), 'x', "Request failed: " + e.message, 14);
        });
    });
  }

  /* ---------- Results page histogram ---------- */
  if ($("histogram") && window.GRADE_TOTALS) {
    var totals = window.GRADE_TOTALS, maxScore = window.GRADE_MAX || 100;
    var buckets = [0, 0, 0, 0, 0];
    var labels = ["<60%", "60-70%", "70-80%", "80-90%", "90-100%"];
    totals.forEach(function (t) {
      var pct = maxScore > 0 ? (t / maxScore) * 100 : 0;
      var b = pct < 60 ? 0 : pct < 70 ? 1 : pct < 80 ? 2 : pct < 90 ? 3 : 4;
      buckets[b]++;
    });
    var maxB = Math.max.apply(null, buckets.concat([1]));
    var html = "";
    buckets.forEach(function (c, i) {
      var w = Math.round((c / maxB) * 100);
      html += '<div class="hist-row"><span class="hist-label">' + labels[i] + '</span>' +
        '<div class="hist-bar-wrap"><div class="hist-bar" style="width:' + w + '%"></div></div>' +
        '<span class="hist-count">' + c + ' students</span></div>';
    });
    $("histogram").innerHTML = html;
  }

  /* ---------- Home: swipe to delete assignments ---------- */
  if (document.querySelector(".swipe-row")) {
    var SWIPE_W = 88; // matches .swipe-delete width in CSS
    var openRow = null;
    function closeOpenRow(except) {
      if (openRow && openRow !== except) {
        openRow.querySelector(".swipe-content").style.transform = "";
        openRow = null;
      }
    }
    function deleteAssignment(id, row) {
      var name = row.querySelector(".task-name").textContent.trim();
      if (!confirm("Delete \"" + name + "\" and all its saved grades? This cannot be undone.")) return;
      fetch("/api/assignment/" + id + "/delete", { method: "POST" })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (!data.ok) { alert("Delete failed: " + (data.error || "unknown error")); return; }
          if (openRow === row) openRow = null;
          row.remove();
        })
        .catch(function (e) { alert("Request failed: " + e.message); });
    }
    document.querySelectorAll(".swipe-row").forEach(function (row) {
      var content = row.querySelector(".swipe-content");
      var startX = 0, dx = 0, dragging = false;
      row.addEventListener("touchstart", function (e) {
        closeOpenRow(row);
        startX = e.touches[0].clientX; dx = 0; dragging = true;
      }, { passive: true });
      row.addEventListener("touchmove", function (e) {
        if (!dragging) return;
        dx = e.touches[0].clientX - startX;
        content.style.transform = dx < 0 ? "translateX(" + Math.max(dx, -SWIPE_W) + "px)" : "";
      }, { passive: true });
      row.addEventListener("touchend", function () {
        dragging = false;
        if (dx < -SWIPE_W / 2) {
          content.style.transform = "translateX(" + (-SWIPE_W) + "px)";
          openRow = row;
        } else {
          content.style.transform = "";
        }
        dx = 0;
      });
    });
    // Tapping outside closes an open row.
    document.addEventListener("touchstart", function (e) {
      if (!e.target.closest(".swipe-row")) closeOpenRow(null);
    }, { passive: true });
    // Both the revealed Delete button and the trash button delete.
    document.querySelectorAll(".swipe-delete, .btn-del").forEach(function (b) {
      b.addEventListener("click", function (e) {
        e.stopPropagation();
        var row = b.closest(".swipe-row");
        deleteAssignment(b.getAttribute("data-id"), row);
      });
    });
  }
})();
