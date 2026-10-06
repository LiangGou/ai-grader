/* AI Grader frontend: example-based rubric generation, structured rubric, photo grading, results histogram */
(function () {
  "use strict";

  function $(id) { return document.getElementById(id); }

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
    row.innerHTML =
      '<div class="rubric-top">' +
        '<input class="q" type="text" placeholder="Question" value="' + escapeHtml(item.q) + '">' +
        '<input class="ms" type="number" step="0.5" min="0" placeholder="Max pts" value="' + escapeHtml(item.max_score) + '">' +
        badge +
        '<button type="button" class="btn btn-small row-del">✕</button>' +
      '</div>' +
      '<label class="rubric-field"><span>Question (digitized)</span>' +
        '<input class="qt" type="text" placeholder="Question text as read from the example" value="' + escapeHtml(item.question_text) + '">' +
        '<div class="math-preview"></div></label>' +
      '<label class="rubric-field"><span>Reference solution (digitized)</span>' +
        '<input class="rs" type="text" placeholder="Reference solution as read from the example" value="' + escapeHtml(item.reference_solution) + '">' +
        '<div class="math-preview"></div></label>' +
      '<label class="rubric-field"><span>Scoring / deduction rules</span>' +
        '<input class="cr" type="text" placeholder="Scoring/deduction rules" value="' + escapeHtml(item.criteria) + '"></label>' +
      '<div class="graph-slot"></div>' +
      note;
    row._graph = item.graph || null;
    row.querySelector(".row-del").addEventListener("click", function () { row.remove(); });
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
        cap2.textContent = "🖼️ Diagram from your example";
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
      var fd = new FormData();
      Array.prototype.forEach.call(fileList, function (f) { fd.append("images", f); });
      $("exampleUploading").style.display = "";
      fetch("/assignment/" + window.ASSIGNMENT_ID + "/upload_rubric_example", { method: "POST", body: fd })
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
    });

    $("generateRubricBtn").addEventListener("click", function () {
      var include = savedExamples.filter(function (ex) { return ex.checked; }).map(function (ex) { return ex.name; });
      if (!include.length) { alert("Please take or upload a reference example first, and make sure at least one saved example is checked."); return; }
      var btn = $("generateRubricBtn");
      btn.disabled = true;
      btn.textContent = "⏳ Digitizing & verifying… (about a minute)";
      fetch("/api/rubric_from_example", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ assignment_id: window.ASSIGNMENT_ID, include: include })
      })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          btn.disabled = false;
          btn.textContent = "✨ Generate Rubric from Example";
          if (!data.ok) { alert("Rubric generation failed: " + data.error); return; }
          rowsBox.innerHTML = "";
          (data.items || []).forEach(function (it) { rowsBox.appendChild(rubricRow(it)); });
          if (!data.items || !data.items.length) {
            alert("Could not read any questions from the example. Try clearer photos, or add rows manually.");
          } else {
            var okCount = data.items.filter(function (it) { return it.verified === "yes"; }).length;
            var needCount = data.items.length - okCount;
            alert("Digitized " + data.items.length + " question(s): " + okCount + " verified ✅" +
              (needCount ? ", " + needCount + " need your review ⚠️" : "") +
              " — please review, edit, then save.");
          }
        })
        .catch(function (e) {
          btn.disabled = false;
          btn.textContent = "✨ Generate Rubric from Example";
          alert("Request failed: " + e.message);
        });
    });

    $("saveRubricBtn").addEventListener("click", function () {
      var items = collectItems();
      if (items.length === 0) {
        alert("The structured rubric is empty. Add a reference example in Step 1, then click “✨ Generate Rubric from Example” first.");
        return;
      }
      var payload = { items: items };
      var btn = $("saveRubricBtn");
      btn.disabled = true;
      btn.textContent = "Saving…";
      fetch("/assignment/" + window.ASSIGNMENT_ID + "/save_rubric", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          btn.disabled = false;
          btn.textContent = "💾 Save Rubric";
          if (!data.ok) { alert("Save failed: " + data.error); return; }
          alert("Rubric saved — grading will use this version.");
        })
        .catch(function (e) {
          btn.disabled = false;
          btn.textContent = "💾 Save Rubric";
          alert("Request failed: " + e.message);
        });
    });
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
      Array.prototype.forEach.call(input.files, function (f) { selectedFiles.push(f); });
      input.value = "";  // allow picking the same file again
      renderPreviews();
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
          overall_feedback: $("overallFeedback").value
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
})();
