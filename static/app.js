/* AI Grader frontend: voice transcription, structured rubric, photo grading, results histogram */
(function () {
  "use strict";

  function $(id) { return document.getElementById(id); }

  /* ---------- Record-then-transcribe voice memos (Whisper) ----------
     window.recordAndTranscribe({container, onText, onError, onCancel}):
     tap record -> animated waveform + timer -> tap stop -> audio POSTed to
     /api/transcribe -> onText(transcript). Manual typing always stays available. */
  window.recordAndTranscribe = function (options) {
    options = options || {};
    var container = options.container;
    var onText = options.onText || function () {};
    var onError = options.onError || function () {};
    var onCancel = options.onCancel || function () {};
    if (!container) { onError("Voice recorder: no container element."); return null; }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      onError("This browser cannot access the microphone. Please type instead.");
      return null;
    }
    if (typeof MediaRecorder === "undefined") {
      onError("Audio recording is not supported in this browser. Please type instead.");
      return null;
    }

    var panel = document.createElement("div");
    panel.className = "rec-panel";
    panel.innerHTML =
      '<div class="rec-top"><span class="rec-dot"></span>' +
      '<span class="rec-timer">0:00</span>' +
      '<span class="rec-hint">Recording… tap Stop when done</span></div>' +
      '<canvas class="rec-wave" width="600" height="120"></canvas>' +
      '<div class="rec-actions">' +
      '<button type="button" class="btn btn-primary btn-small rec-stop">⏹ Stop</button>' +
      '<button type="button" class="btn btn-small rec-cancel">Cancel</button></div>';
    container.innerHTML = "";
    container.appendChild(panel);
    var canvas = panel.querySelector(".rec-wave");
    var timerEl = panel.querySelector(".rec-timer");
    var hintEl = panel.querySelector(".rec-hint");
    var stopBtn = panel.querySelector(".rec-stop");
    var cancelBtn = panel.querySelector(".rec-cancel");

    var stream = null, recorder = null, audioCtx = null, analyser = null;
    var rafId = null, timerId = null, chunks = [], startTime = 0, finished = false;

    function cleanup() {
      finished = true;
      if (rafId) { cancelAnimationFrame(rafId); rafId = null; }
      if (timerId) { clearInterval(timerId); timerId = null; }
      try { if (audioCtx) audioCtx.close(); } catch (e) { /* noop */ }
      audioCtx = null;
      if (stream) { stream.getTracks().forEach(function (t) { t.stop(); }); stream = null; }
    }
    function fail(msg) {
      cleanup();
      panel.innerHTML = "";
      onError(msg);
    }
    function pickMime() {
      var cands = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus", "audio/mp4;codecs=mp4a"];
      for (var i = 0; i < cands.length; i++) {
        try { if (MediaRecorder.isTypeSupported(cands[i])) return cands[i]; } catch (e) { /* noop */ }
      }
      return "";
    }
    function draw() {
      if (finished || !analyser) return;
      var ctx = canvas.getContext("2d");
      var W = canvas.width, H = canvas.height;
      ctx.clearRect(0, 0, W, H);
      var data = new Uint8Array(analyser.frequencyBinCount);
      analyser.getByteFrequencyData(data);
      var n = data.length, bw = W / n;
      for (var i = 0; i < n; i++) {
        var v = data[i] / 255, h = Math.max(3, v * H);
        ctx.fillStyle = "hsl(" + (210 - Math.round(v * 60)) + ", 80%, 55%)";
        ctx.fillRect(i * bw + 1, (H - h) / 2, Math.max(1, bw - 2), h);
      }
      rafId = requestAnimationFrame(draw);
    }
    function tick() {
      var s = Math.floor((Date.now() - startTime) / 1000);
      timerEl.textContent = Math.floor(s / 60) + ":" + ("0" + (s % 60)).slice(-2);
    }
    function send(blob) {
      hintEl.textContent = "Transcribing…";
      stopBtn.disabled = true;
      cancelBtn.disabled = true;
      var fd = new FormData();
      var ext = (blob.type.indexOf("mp4") !== -1 || blob.type.indexOf("m4a") !== -1) ? "m4a" : "webm";
      fd.append("audio", blob, "memo." + ext);
      fetch("/api/transcribe", { method: "POST", body: fd })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          cleanup();
          panel.innerHTML = "";
          if (!data.ok) { onError(data.error || "Transcription failed. Please try again."); return; }
          onText(data.text || "");
        })
        .catch(function (e) {
          cleanup();
          panel.innerHTML = "";
          onError("Transcription request failed: " + e.message);
        });
    }

    stopBtn.addEventListener("click", function () {
      if (finished || !recorder) return;
      finished = true;
      try { recorder.stop(); }
      catch (e) { fail("Could not stop the recording. Please try again."); }
    });
    cancelBtn.addEventListener("click", function () {
      cleanup();
      panel.innerHTML = "";
      onCancel();
    });

    navigator.mediaDevices.getUserMedia({ audio: true }).then(function (s) {
      stream = s;
      var mime = pickMime();
      try {
        recorder = mime ? new MediaRecorder(stream, { mimeType: mime }) : new MediaRecorder(stream);
      } catch (e) { fail("Could not start the recorder: " + e.message); return; }
      recorder.ondataavailable = function (e) { if (e.data && e.data.size) chunks.push(e.data); };
      recorder.onstop = function () {
        if (rafId) { cancelAnimationFrame(rafId); rafId = null; }
        if (timerId) { clearInterval(timerId); timerId = null; }
        try { if (audioCtx) audioCtx.close(); } catch (e2) { /* noop */ }
        audioCtx = null;
        var blob = new Blob(chunks, { type: recorder.mimeType || "audio/webm" });
        stream.getTracks().forEach(function (t) { t.stop(); });
        stream = null;
        if (!blob.size) { panel.innerHTML = ""; onError("The recording is empty. Please try again."); return; }
        send(blob);
      };
      recorder.onerror = function () { fail("Recording error. Please check the microphone and try again."); };
      try {
        audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        var src = audioCtx.createMediaStreamSource(stream);
        analyser = audioCtx.createAnalyser();
        analyser.fftSize = 64;
        analyser.smoothingTimeConstant = 0.75;
        src.connect(analyser);
      } catch (e) { fail("Could not read microphone audio: " + e.message); return; }
      chunks = [];
      try { recorder.start(250); }
      catch (e) { fail("Could not start the recorder: " + e.message); return; }
      startTime = Date.now();
      tick();
      timerId = setInterval(tick, 250);
      draw();
    }).catch(function (e) {
      var msg = "Microphone access was denied. Please allow microphone access and try again.";
      if (e && e.name === "NotFoundError") msg = "No microphone found on this device.";
      else if (e && e.name === "NotAllowedError") msg = "Microphone access was denied. Please allow it in the browser settings and try again.";
      fail(msg);
    });

    return { cancel: function () { if (!finished) { cleanup(); panel.innerHTML = ""; onCancel(); } } };
  };

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

  if ($("itemsRows") && typeof window.ASSIGNMENT_ID !== "undefined" && $("structureBtn")) {
    var rowsBox = $("itemsRows");
    (window.EXISTING_ITEMS || []).forEach(function (it) { rowsBox.appendChild(rubricRow(it)); });

    function setVoiceStatus(msg, isError) {
      var s = $("voiceStatus");
      if (s) {
        s.textContent = msg;
        s.classList.toggle("error-text", !!isError);
      }
    }
    $("voiceBtn").addEventListener("click", function () {
      var btn = this;
      btn.disabled = true;
      setVoiceStatus("Starting recorder…", false);
      window.recordAndTranscribe({
        container: $("recPanel"),
        onText: function (text) {
          btn.disabled = false;
          setVoiceStatus(text ? "Transcribed — review the notes below, then generate the structured rubric." : "Got empty transcription — please try again.", !text);
          var ta = $("transcript");
          ta.value = ta.value ? ta.value.replace(/\s+$/, "") + "\n" + text : text;
        },
        onError: function (msg) { btn.disabled = false; setVoiceStatus(msg, true); },
        onCancel: function () { btn.disabled = false; setVoiceStatus("Recording cancelled.", false); }
      });
    });

    $("addRowBtn").addEventListener("click", function () {
      rowsBox.appendChild(rubricRow());
    });

    $("structureBtn").addEventListener("click", function () {
      var transcript = $("transcript").value.trim();
      if (!transcript) { alert("Please record or type the grading rules first."); return; }
      var btn = $("structureBtn");
      btn.disabled = true;
      btn.textContent = "⏳ Structuring with AI…";
      fetch("/api/structure_rubric", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ transcript: transcript })
      })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          btn.disabled = false;
          btn.textContent = "✨ Generate Structured Rubric";
          if (!data.ok) { alert("Structuring failed: " + data.error); return; }
          rowsBox.innerHTML = "";
          (data.items || []).forEach(function (it) { rowsBox.appendChild(rubricRow(it)); });
          if (!data.items || !data.items.length) alert("Could not parse any questions from the notes. Please check the notes or add rows manually.");
          else alert("Generated " + data.items.length + " question(s) — please review, edit, then save.");
        })
        .catch(function (e) {
          btn.disabled = false;
          btn.textContent = "✨ Generate Structured Rubric";
          alert("Request failed: " + e.message);
        });
    });

    $("saveRubricBtn").addEventListener("click", function () {
      var items = collectItems();
      var payload;
      if (items.length > 0) {
        payload = { mode: "structured", items: items };
      } else {
        var raw = $("rawText").value.trim();
        if (!raw) { alert("The rubric is empty: generate the structured table by voice, or type in the plain-text box."); return; }
        payload = { mode: "text", raw_text: raw };
      }
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
    $("nameVoiceBtn").addEventListener("click", function () {
      var btn = this;
      btn.disabled = true;
      window.recordAndTranscribe({
        container: $("nameRecPanel"),
        onText: function (text) {
          btn.disabled = false;
          $("studentName").value = (text || "").trim();
        },
        onError: function (msg) { btn.disabled = false; alert(msg); },
        onCancel: function () { btn.disabled = false; }
      });
    });
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
      if (!name) { alert("Please enter the student name first."); return; }
      if (!input.files.length) { alert("Please take/upload paper photos first."); return; }
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
