/* AI Grader frontend: example-based rubric generation, structured rubric, photo grading, results histogram */
(function () {
  "use strict";

  function $(id) { return document.getElementById(id); }

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
  function setSaveStatus(text, isError) {
    var el = $("rubricSaveStatus");
    if (!el) return;
    el.textContent = text;
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
        setSaveStatus("Auto-saved ✓", false);
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
      ? '<span class="verify-badge ok">✅ Verified</span>'
      : state === "warn"
        ? '<span class="verify-badge warn">⚠️ Needs your review</span>'
        : '<span class="verify-badge manual">✏️ Manual</span>';
    var note = (state === "warn" && item.verify_note)
      ? '<p class="verify-note">' + escapeHtml(item.verify_note) + '</p>' : "";
    // Retry panel for AI-solved questions that failed verification.
    // Shows for ai_solved items, or older items with no solution to lose.
    var canRetry = item.origin === "ai_solved" ||
      (!item.origin && state === "warn" && !(item.reference_solution || "").trim());
    var retryHtml = "";
    if (canRetry) {
      retryHtml =
        '<div class="retry-box">' +
          '<button type="button" class="btn btn-small retry-toggle">🔄 Retry with AI</button>' +
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
        '<button type="button" class="btn btn-small row-del">✕</button>' +
      '</div>' +
      '<label class="rubric-field"><span>Question (digitized)</span>' +
        '<textarea class="qt" rows="2" placeholder="Question text as read from the example">' + escapeHtml(item.question_text) + '</textarea>' +
        '<div class="math-preview"></div></label>' +
      '<label class="rubric-field"><span>Reference solution (digitized)</span>' +
        '<textarea class="rs" rows="3" placeholder="Reference solution as read from the example">' + escapeHtml(item.reference_solution) + '</textarea>' +
        '<div class="math-preview"></div></label>' +
      '<label class="rubric-field"><span>Scoring / deduction rules</span>' +
        '<textarea class="cr" rows="3" placeholder="Scoring/deduction rules">' + escapeHtml(item.criteria) + '</textarea></label>' +
      '<div class="graph-slot"></div>' +
      note +
      retryHtml;
    row._graph = item.graph || null;
    row.querySelector(".row-del").addEventListener("click", function () { row.remove(); scheduleAutoSave(); });
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
          .then(function (r) { return r.json(); })
          .then(function (data) {
            goBtn.disabled = false;
            if (!data.ok) {
              status.textContent = "❌ " + (data.error || "retry failed");
              return;
            }
            var res = data.result;
            // Show debug output.
            rawPre.textContent = (res.debug || []).map(function (d) {
              return "=== " + d.step + " (" + d.model + (d.effort ? ", effort=" + d.effort : "") + ") ===\n" + d.raw;
            }).join("\n\n");
            row.querySelector(".retry-debug").open = true;
            if (res.verified === "yes") {
              status.textContent = "✅ Verified on retry — solution and rubric updated.";
            } else {
              status.textContent = "⚠️ Still needs review: " + (res.verify_note || "");
            }
            // Update the card with the new solution.
            row.querySelector(".rs").value = res.solution || "";
            row.querySelector(".rs").dispatchEvent(new Event("input"));
            row.querySelector(".cr").value = res.criteria || "";
            var b = row.querySelector(".verify-badge");
            if (b) {
              b.className = "verify-badge " + (res.verified === "yes" ? "ok" : "warn");
              b.textContent = res.verified === "yes" ? "✅ Verified" : "⚠️ Needs your review";
            }
            var vn = row.querySelector(".verify-note");
            if (vn) vn.textContent = res.verify_note || "";
            scheduleAutoSave();
          })
          .catch(function (e) {
            goBtn.disabled = false;
            status.textContent = "❌ Request failed: " + e.message;
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
        cap.textContent = "📈 Regenerated graph" + (g.expression ? ": y = " + g.expression : "");
        var cv = document.createElement("canvas");
        cv.className = "graph-canvas";
        cv.width = 600; cv.height = 360;
        slot.appendChild(cap);
        slot.appendChild(cv);
        drawFunctionGraph(cv, g);
      } else if (g.kind === "image" && g.url) {
        var cap2 = document.createElement("p");
        cap2.className = "graph-cap";
        cap2.textContent = "🤖 AI-generated diagram";
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
            b.textContent = "✅ Verified by you";
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

  if ($("itemsRows") && typeof window.ASSIGNMENT_ID !== "undefined" && $("generateRubricBtn")) {
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
        del.textContent = "✕";
        del.title = "Delete this example";
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
      btn.textContent = "🔑 Generate Rubric";
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
        alert("Digitized " + items.length + " question(s): " + okCount + " verified ✅" +
          (needCount ? ", " + needCount + " need your review ⚠️" : "") +
          " — auto-saved. Every edit auto-saves too.");
      }
    }

    function failGenerate(btn, msg) {
      btn.disabled = false;
      btn.textContent = "🔑 Generate Rubric";
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
              btn.textContent = total > 0 ? "⏳ Validated " + nDone + "/" + total + " questions…" : "⏳ Reading questions… (" + elapsed + "s)";
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
    // from the printed point values.
    $("solveRubricBtn").addEventListener("click", function () {
      var include = savedExamples.filter(function (ex) { return ex.checked; }).map(function (ex) { return ex.name; });
      if (!include.length) { alert("Please take or upload the question photos first, and make sure at least one saved example is checked."); return; }
      var btn = $("solveRubricBtn");
      btn.disabled = true;
      btn.textContent = "⏳ Generating rubric…";
      rowsBox.innerHTML = ""; // fresh list; questions stream in one by one
      renderedGenIdx = {};
      fetch("/api/solve_rubric", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ assignment_id: window.ASSIGNMENT_ID, include: include })
      })
        .then(function (r) { return r.json(); })
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
      selectedFiles.forEach(function (f) {
        var img = document.createElement("img");
        img.className = "thumb";
        img.src = URL.createObjectURL(f);
        preview.appendChild(img);
      });
    }
    function handlePicked(input) {
      var files = Array.prototype.slice.call(input.files);
      input.value = "";  // allow picking the same file again
      if (!files.length) return;
      Promise.all(files.map(shrinkImage)).then(function (shrunk) {
        shrunk.forEach(function (f) { selectedFiles.push(f); });
        renderPreviews();
      });
    }
    $("photoInput").addEventListener("change", function () { handlePicked(this); });
    $("libraryInput").addEventListener("change", function () { handlePicked(this); });

    var currentResult = null;

    $("gradeBtn").addEventListener("click", function () {
      var name = $("studentName").value.trim();
      if (!name) { alert("Please enter the student name first."); return; }
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
          $("saveMsg").textContent = data.ok ? "✅ Saved — you can grade the next student." : "Save failed: " + data.error;
          if (data.ok) { $("studentName").value = ""; selectedFiles = []; renderPreviews(); $("gradeResult").style.display = "none"; currentResult = null; }
        })
        .catch(function (e) {
          btn.disabled = false;
          $("saveMsg").textContent = "Request failed: " + e.message;
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
    // Both the revealed Delete button and the 🗑️ button delete.
    document.querySelectorAll(".swipe-delete, .btn-del").forEach(function (b) {
      b.addEventListener("click", function (e) {
        e.stopPropagation();
        var row = b.closest(".swipe-row");
        deleteAssignment(b.getAttribute("data-id"), row);
      });
    });
  }
})();
