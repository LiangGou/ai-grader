/* AI Grader frontend: example-based rubric generation, structured rubric, photo grading, results histogram */
(function () {
  "use strict";

  function $(id) { return document.getElementById(id); }

  /* ---------- Assignment setup page: structured rubric ---------- */
  function rubricRow(item) {
    item = item || { q: "", max_score: "", criteria: "" };
    var row = document.createElement("div");
    row.className = "rubric-row";
    row.innerHTML =
      '<input class="q" type="text" placeholder="Question" value="' + escapeHtml(item.q) + '">' +
      '<input class="ms" type="number" step="0.5" min="0" placeholder="Max pts" value="' + escapeHtml(item.max_score) + '">' +
      '<input class="cr grow" type="text" placeholder="Scoring/deduction rules" value="' + escapeHtml(item.criteria) + '">' +
      '<button type="button" class="btn btn-small row-del">✕</button>';
    row.querySelector(".row-del").addEventListener("click", function () { row.remove(); });
    return row;
  }
  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
  function collectItems() {
    var items = [];
    document.querySelectorAll("#itemsRows .rubric-row").forEach(function (row) {
      var q = row.querySelector(".q").value.trim();
      if (!q) return;
      items.push({
        q: q,
        max_score: parseFloat(row.querySelector(".ms").value) || 0,
        criteria: row.querySelector(".cr").value.trim()
      });
    });
    return items;
  }

  if ($("itemsRows") && typeof window.ASSIGNMENT_ID !== "undefined" && $("generateRubricBtn")) {
    var rowsBox = $("itemsRows");
    (window.EXISTING_ITEMS || []).forEach(function (it) { rowsBox.appendChild(rubricRow(it)); });

    var exampleFiles = [];
    function renderExamplePreviews() {
      var preview = $("examplePreview");
      preview.innerHTML = "";
      exampleFiles.forEach(function (f) {
        var img = document.createElement("img");
        img.className = "thumb";
        img.src = URL.createObjectURL(f);
        preview.appendChild(img);
      });
    }
    function handleExamplePicked(input) {
      Array.prototype.forEach.call(input.files, function (f) { exampleFiles.push(f); });
      input.value = "";
      renderExamplePreviews();
    }
    $("examplePhotoInput").addEventListener("change", function () { handleExamplePicked(this); });
    $("exampleLibraryInput").addEventListener("change", function () { handleExamplePicked(this); });

    $("addRowBtn").addEventListener("click", function () {
      rowsBox.appendChild(rubricRow());
    });

    $("generateRubricBtn").addEventListener("click", function () {
      if (!exampleFiles.length) { alert("Please take or upload a reference example first (a hand-graded paper, answer key, or scoring guide)."); return; }
      var btn = $("generateRubricBtn");
      btn.disabled = true;
      btn.textContent = "⏳ Reading example with AI…";
      var fd = new FormData();
      exampleFiles.forEach(function (f) { fd.append("images", f); });
      fetch("/api/rubric_from_example", { method: "POST", body: fd })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          btn.disabled = false;
          btn.textContent = "✨ Generate Rubric from Example";
          if (!data.ok) { alert("Rubric generation failed: " + data.error); return; }
          rowsBox.innerHTML = "";
          (data.items || []).forEach(function (it) { rowsBox.appendChild(rubricRow(it)); });
          if (!data.items || !data.items.length) alert("Could not read any questions from the example. Try clearer photos, or add rows manually.");
          else alert("Generated " + data.items.length + " question(s) — please review, edit, then save.");
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
          if (data.ok) { $("studentName").value = ""; input.value = ""; preview.innerHTML = ""; $("gradeResult").style.display = "none"; currentResult = null; }
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
