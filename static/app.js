/* AI 判卷前端交互：语音转写、结构化 rubric、拍照判分、成绩统计直方图 */
(function () {
  "use strict";

  function $(id) { return document.getElementById(id); }

  /* ---------- Web Speech API 语音转写（zh-CN） ---------- */
  function bindVoice(btnId, textareaId, statusId) {
    var btn = $(btnId), stopBtn = $("stopVoiceBtn"), ta = $(textareaId);
    if (!btn || !ta) return;
    var SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) {
      btn.disabled = true;
      btn.textContent = "🎤 当前浏览器不支持语音输入";
      return;
    }
    var rec = new SR();
    rec.lang = "zh-CN";
    rec.interimResults = true;
    rec.continuous = true;
    var baseText = "";
    rec.onresult = function (e) {
      var interim = "";
      for (var i = e.resultIndex; i < e.results.length; i++) {
        var t = e.results[i][0].transcript;
        if (e.results[i].isFinal) baseText += t;
        else interim += t;
      }
      ta.value = (baseText + interim).trim();
    };
    rec.onend = function () {
      btn.style.display = "";
      if (stopBtn) stopBtn.style.display = "none";
      setStatus("转写结束，可继续点按钮补充，或直接编辑文本框。");
    };
    rec.onerror = function (e) {
      setStatus("语音识别出错：" + e.error + "，请检查麦克风权限后重试。");
      btn.style.display = "";
      if (stopBtn) stopBtn.style.display = "none";
    };
    function setStatus(msg) {
      var s = statusId ? $(statusId) : null;
      if (s) s.textContent = msg;
    }
    btn.addEventListener("click", function () {
      baseText = ta.value ? ta.value.trim() + "，" : "";
      try {
        rec.start();
        btn.style.display = "none";
        if (stopBtn) { stopBtn.style.display = ""; stopBtn.onclick = function () { rec.stop(); }; }
        setStatus("🎙 正在聆听…说完后点「停止」。");
      } catch (err) { setStatus("启动语音识别失败：" + err.message); }
    });
  }

  /* ---------- 任务设置页：结构化 rubric ---------- */
  function rubricRow(item) {
    item = item || { q: "", max_score: "", criteria: "" };
    var row = document.createElement("div");
    row.className = "rubric-row";
    row.innerHTML =
      '<input class="q" type="text" placeholder="题号" value="' + escapeHtml(item.q) + '">' +
      '<input class="ms" type="number" step="0.5" min="0" placeholder="满分" value="' + escapeHtml(item.max_score) + '">' +
      '<input class="cr grow" type="text" placeholder="得分/扣分规则" value="' + escapeHtml(item.criteria) + '">' +
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

  if ($("itemsRows") && typeof window.ASSIGNMENT_ID !== "undefined" && $("structureBtn")) {
    var rowsBox = $("itemsRows");
    (window.EXISTING_ITEMS || []).forEach(function (it) { rowsBox.appendChild(rubricRow(it)); });

    bindVoice("voiceBtn", "transcript", "voiceStatus");

    $("addRowBtn").addEventListener("click", function () {
      rowsBox.appendChild(rubricRow());
    });

    $("structureBtn").addEventListener("click", function () {
      var transcript = $("transcript").value.trim();
      if (!transcript) { alert("请先语音录入或输入判分规则。"); return; }
      var btn = $("structureBtn");
      btn.disabled = true;
      btn.textContent = "⏳ AI 整理中…";
      fetch("/api/structure_rubric", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ transcript: transcript })
      })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          btn.disabled = false;
          btn.textContent = "✨ 生成结构化 Rubric";
          if (!data.ok) { alert("整理失败：" + data.error); return; }
          rowsBox.innerHTML = "";
          (data.items || []).forEach(function (it) { rowsBox.appendChild(rubricRow(it)); });
          if (!data.items || !data.items.length) alert("没能从记录中解析出题目，请检查记录内容或手动添加。");
          else alert("已生成 " + data.items.length + " 题，请检查修改后保存。");
        })
        .catch(function (e) {
          btn.disabled = false;
          btn.textContent = "✨ 生成结构化 Rubric";
          alert("请求失败：" + e.message);
        });
    });

    $("saveRubricBtn").addEventListener("click", function () {
      var items = collectItems();
      var payload;
      if (items.length > 0) {
        payload = { mode: "structured", items: items };
      } else {
        var raw = $("rawText").value.trim();
        if (!raw) { alert("rubric 是空的：请语音生成结构化表格，或在纯文本框里打字。"); return; }
        payload = { mode: "text", raw_text: raw };
      }
      var btn = $("saveRubricBtn");
      btn.disabled = true;
      btn.textContent = "保存中…";
      fetch("/assignment/" + window.ASSIGNMENT_ID + "/save_rubric", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          btn.disabled = false;
          btn.textContent = "💾 保存 Rubric";
          if (!data.ok) { alert("保存失败：" + data.error); return; }
          alert("Rubric 已保存，判分将使用该版本。");
        })
        .catch(function (e) {
          btn.disabled = false;
          btn.textContent = "💾 保存 Rubric";
          alert("请求失败：" + e.message);
        });
    });
  }

  /* ---------- 判卷页 ---------- */
  if ($("gradeBtn") && typeof window.ASSIGNMENT_ID !== "undefined") {
    bindVoice("nameVoiceBtn", "studentName", null);
    var input = $("photoInput"), preview = $("preview");
    input.addEventListener("change", function () {
      preview.innerHTML = "";
      Array.prototype.forEach.call(input.files, function (f) {
        var img = document.createElement("img");
        img.className = "thumb";
        img.src = URL.createObjectURL(f);
        preview.appendChild(img);
      });
    });

    var currentResult = null;

    $("gradeBtn").addEventListener("click", function () {
      var name = $("studentName").value.trim();
      if (!name) { alert("请先填写学生姓名。"); return; }
      if (!input.files.length) { alert("请先拍照上传答卷。"); return; }
      var fd = new FormData();
      fd.append("assignment_id", window.ASSIGNMENT_ID);
      fd.append("student_name", name);
      Array.prototype.forEach.call(input.files, function (f) { fd.append("images", f); });
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
            eb.textContent = "判分失败：" + data.error;
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
          eb.textContent = "请求失败：" + e.message;
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
          '<span><input type="number" class="score-input" data-i="' + i + '" step="0.5" min="0" max="' + q.max_score + '" value="' + q.score + '"> / ' + q.max_score + ' 分</span></div>' +
          (q.answer_seen ? '<p class="q-seen">识别到的作答：' + escapeHtml(q.answer_seen) + '</p>' : "") +
          '<p class="q-feedback">' + escapeHtml(q.feedback) + '</p>';
        box.appendChild(div);
      });
      $("totalInput").value = result.total;
      $("totalInput").max = result.max_total;
      $("maxTotal").textContent = result.max_total + " 分";
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
    $("totalInput").addEventListener("input", function () { /* 总分允许手动微调 */ });

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
          $("saveMsg").textContent = data.ok ? "✅ 已保存，可继续判下一位学生。" : "保存失败：" + data.error;
          if (data.ok) { $("studentName").value = ""; input.value = ""; preview.innerHTML = ""; $("gradeResult").style.display = "none"; currentResult = null; }
        })
        .catch(function (e) {
          btn.disabled = false;
          $("saveMsg").textContent = "请求失败：" + e.message;
        });
    });
  }

  /* ---------- 成绩页直方图 ---------- */
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
        '<span class="hist-count">' + c + '人</span></div>';
    });
    $("histogram").innerHTML = html;
  }
})();
