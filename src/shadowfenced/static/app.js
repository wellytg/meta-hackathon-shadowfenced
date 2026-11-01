/* Shadowfenced demo UI: Tier 1 scan underlines, issue cards, decision banner. */
(function () {
  "use strict";

  var composer = document.getElementById("composer");
  var backdrop = document.getElementById("backdrop");
  var issuesBox = document.getElementById("issues");
  var sendBtn = document.getElementById("send");
  var checking = document.getElementById("checking");
  var thread = document.getElementById("thread");
  var providerSel = document.getElementById("provider");
  var headerPill = document.querySelector('[data-testid="header-pill"]');
  var paperclip = document.getElementById("paperclip");
  var fileInput = document.getElementById("file-input");
  var fileChips = document.getElementById("file-chips");
  var attachedFiles = []; // {file_id, name, kind, status, summary_text, spans}

  var PROVIDER_LABELS = {
    meta: "Meta Muse Spark (cloud)",
    ollama: "Llama 3.2 3B via Ollama (local stand-in)"
  };

  function providerLabel(id) {
    return PROVIDER_LABELS[id] || String(id);
  }

  var HEADER_PILL = "Local check \u00b7 code + Muse Glimmer (30B, this PC)";

  // Live model hints are experimental and off by default: the server
  // says whether they are on (GET /api/health returns live_hints).
  var liveHintsOn = false;

  function setPill() {
    if (!headerPill) return;
    headerPill.textContent = HEADER_PILL;
  }

  // Hosted demo (SF_HOSTED=1): reveal the fixed public-demo banner.
  function loadHostedBanner() {
    fetch("/api/hosted")
      .then(function (resp) { return resp.json(); })
      .then(function (body) {
        if (body && body.hosted) {
          var el = document.getElementById("hosted-banner");
          if (el) el.hidden = false;
        }
      })
      .catch(function () { /* banner stays hidden */ });
  }

  function loadLiveHintsFlag() {
    fetch("/api/health")
      .then(function (resp) { return resp.json(); })
      .then(function (body) {
        liveHintsOn = !!(body && body.live_hints);
      })
      .catch(function () { /* hints stay off */ });
  }

  function loadProviders() {
    if (!providerSel) return;
    fetch("/api/providers")
      .then(function (resp) { return resp.json(); })
      .then(function (body) {
        var list = body.providers || [];
        providerSel.innerHTML = "";
        list.forEach(function (p) {
          var opt = document.createElement("option");
          opt.value = p.id;
          opt.textContent = p.available ? p.label : p.label + " (no API key)";
          opt.disabled = !p.available;
          providerSel.appendChild(opt);
        });
        if (body.default) providerSel.value = body.default;
        // If the saved default is unavailable, fall back to ollama.
        if (providerSel.selectedIndex < 0 ||
            providerSel.options[providerSel.selectedIndex].disabled) {
          var fallback = list.filter(function (p) { return p.available; })[0];
          if (fallback) providerSel.value = fallback.id;
        }
        setPill();
      })
      .catch(function () { /* static options stay as the fallback */ });
  }

  providerSel.addEventListener("change", function () {
    fetch("/api/provider", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: providerSel.value })
    })
      .then(function () { setPill(); })
      .catch(function () { /* default stays server-side */ });
  });

  var scanSeq = 0;       // latest scan request id; stale responses are dropped
  var liveSeq = 0;       // latest live-scan id; stale responses are dropped
  var spans = [];        // spans from the latest accepted scan
  var liveSpans = [];    // tier-2 model suspicions from the latest live scan
  var choices = {};      // span_id -> "redact" | "keep", only spans the user clicked
  var scanTimer = null;
  var liveTimer = null;
  var LIVE_TOOLTIP = "Model suspicion (Llama 3.2, local) \u2014 checked again at send";
  var emptyHint = document.getElementById("empty-hint");

  function updateEmptyHint() {
    if (!emptyHint) return;
    emptyHint.hidden = thread.querySelector(".bubble, .banner") !== null;
  }

  function escapeHtml(text) {
    return text
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  // Underline class: red for secrets, orange for pii/business,
  // purple dotted for beacons, anything ambiguous (needs review),
  // and tier-2 live model suspicions.
  function markClass(span) {
    if (span.tier === 2) return "mark-live";
    if (span.status === "ambiguous") return "mark-review";
    if (span.cls === "secret") return "mark-secret";
    if (span.cls === "pii") return "mark-pii";
    if (span.cls === "business") return "mark-business";
    return "mark-beacon";
  }

  function testIdFor(span) {
    return "mark-" + span.cls;
  }

  // Offsets from /api/scan are UTF-16 code units, which is exactly how
  // JS strings index, so plain slice() aligns with the backend spans.
  function renderBackdrop(text, liveSpans) {
    var ordered = liveSpans.slice().sort(function (a, b) { return a.start - b.start; });
    var html = "";
    var cursor = 0;
    ordered.forEach(function (span) {
      if (span.start < cursor || span.start >= span.end) return;
      html += escapeHtml(text.slice(cursor, span.start));
      var liveTip = span.tier === 2 ?
        ' title="' + escapeHtml(LIVE_TOOLTIP) + '"' : "";
      html += '<mark data-testid="' + escapeHtml(testIdFor(span)) + '"' +
        ' data-span-id="' + escapeHtml(span.span_id) + '"' +
        ' class="' + escapeHtml(markClass(span)) + '"' + liveTip + '>' +
        escapeHtml(text.slice(span.start, span.end)) + "</mark>";
      cursor = span.end;
    });
    html += escapeHtml(text.slice(cursor));
    // Trailing newline keeps the backdrop height identical to the textarea.
    backdrop.innerHTML = html + "\n";
    syncScroll();
  }

  function syncScroll() {
    backdrop.scrollTop = composer.scrollTop;
    backdrop.scrollLeft = composer.scrollLeft;
  }

  var ICONS = { secret: "!", pii: "@", business: "#", beacon: "?" };

  function renderIssues(liveSpans, latency) {
    issuesBox.innerHTML = "";
    liveSpans.forEach(function (span) {
      var card = document.createElement("div");
      card.className = "issue-card" + (span.cls === "secret" ? " sev-secret" : "") +
        (span.status === "ambiguous" ? " sev-review" : "");

      var head = document.createElement("div");
      head.className = "issue-head";
      var icon = document.createElement("span");
      icon.className = "issue-icon";
      icon.textContent = ICONS[span.cls] || "•";
      var msg = document.createElement("span");
      msg.className = "issue-msg";
      msg.textContent = span.message || (span.cls + " / " + span.subclass);
      head.appendChild(icon);
      head.appendChild(msg);
      card.appendChild(head);

      var actions = document.createElement("div");
      actions.className = "issue-actions";

      var redact = document.createElement("button");
      redact.type = "button";
      redact.setAttribute("data-testid", "btn-redact");
      redact.textContent = "Redact";
      redact.setAttribute("aria-pressed", choices[span.span_id] !== "keep" ? "true" : "false");
      redact.addEventListener("click", function () {
        choices[span.span_id] = "redact";
        renderIssues(spans, latency);
      });

      var keep = document.createElement("button");
      keep.type = "button";
      keep.setAttribute("data-testid", "btn-keep");
      keep.textContent = "Keep";
      if (!span.can_keep) keep.disabled = true;
      keep.setAttribute("aria-pressed", choices[span.span_id] === "keep" ? "true" : "false");
      keep.addEventListener("click", function () {
        choices[span.span_id] = "keep";
        renderIssues(spans, latency);
      });

      var why = document.createElement("button");
      why.type = "button";
      why.setAttribute("data-testid", "btn-why");
      why.textContent = "Why";
      var whyText = document.createElement("div");
      whyText.className = "why-text";
      whyText.setAttribute("data-testid", "why-text");
      var tierMs = latency && latency.tier1 != null ? Number(latency.tier1).toFixed(1) + " ms" : "—";
      whyText.textContent = "tier " + span.tier + " · proof " + span.proof +
        " · " + (span.why || span.subclass) + " · tier1 " + tierMs;
      whyText.hidden = true;
      why.addEventListener("click", function () {
        whyText.hidden = !whyText.hidden;
      });

      actions.appendChild(redact);
      actions.appendChild(keep);
      actions.appendChild(why);
      card.appendChild(actions);
      card.appendChild(whyText);
      issuesBox.appendChild(card);
    });
  }

  function scanNow(live) {
    var text = composer.value;
    if (!text) {
      if (!live) {
        spans = [];
        renderBackdrop(text, spans);
        renderIssues(spans, null);
      }
      liveSpans = [];
      return;
    }
    if (live) {
      // No purple model hints unless the server says they are on.
      if (!liveHintsOn) {
        liveSpans = [];
        return;
      }
      var myLive = ++liveSeq;
      fetch("/api/scan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: text, mode: "live" })
      })
        .then(function (resp) { return resp.json(); })
        .then(function (body) {
          if (myLive !== liveSeq) return; // stale response
          if (composer.value !== text) return; // stale text
          liveSpans = (body.spans || []).filter(function (s) {
            return s.tier === 2;
          });
          renderBackdrop(composer.value, spans.concat(liveSpans));
        })
        .catch(function () { /* live hints are advisory */ });
      return;
    }
    var mySeq = ++scanSeq;
    fetch("/api/scan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: text, mode: "tier1" })
    })
      .then(function (resp) { return resp.json(); })
      .then(function (body) {
        if (mySeq !== scanSeq) return; // stale response
        if (composer.value !== text) return; // stale text
        spans = body.spans || [];
        // A fresh tier-1 pass invalidates older live hints; they come
        // back on the next typing pause.
        liveSpans = [];
        // No default choices: only spans the user explicitly clicks are
        // sent, so the gate never reports a choice the user never made.
        choices = {};
        renderBackdrop(text, spans);
        renderIssues(spans, body.latency_ms);
      })
      .catch(function () { /* scan is advisory; send still gates */ });
  }

  function queueScan() {
    if (scanTimer) clearTimeout(scanTimer);
    scanTimer = setTimeout(function () { scanNow(false); }, 250);
    // Live model hints arrive after a 1200 ms typing pause, but only
    // when the server says they are on (SF_LIVE_HINTS=on).
    if (liveTimer) clearTimeout(liveTimer);
    if (liveHintsOn) {
      liveTimer = setTimeout(function () { scanNow(true); }, 1200);
    }
  }

  function highlightPlaceholders(text) {
    return escapeHtml(text).replace(
      /(\[[A-Z0-9 _-]*\d[^\]]*\])/g, '<span class="ph">$1</span>');
  }

  // Highlights use code-point offsets; Array.from splits by code point.
  function highlightRanges(text, ranges) {
    var chars = Array.from(text);
    var marks = [];
    (ranges || []).forEach(function (r) {
      if (r.start < r.end) marks.push(r);
    });
    marks.sort(function (a, b) { return a.start - b.start; });
    var html = "";
    var cursor = 0;
    marks.forEach(function (r) {
      html += escapeHtml(chars.slice(cursor, r.start).join(""));
      html += '<span class="hl">' + escapeHtml(chars.slice(r.start, r.end).join("")) + "</span>";
      cursor = r.end;
    });
    html += escapeHtml(chars.slice(cursor).join(""));
    return html;
  }

  // User-facing banner line. The server's user_message is plain text
  // with no span ids, hashes, or raw values; machine reasons stay in
  // the log and are never shown here.
  function bannerText(decision, body) {
    if (body && typeof body.user_message === "string" && body.user_message) {
      return body.user_message;
    }
    if (decision === "ALLOW") return "Sent clean";
    if (decision === "BLOCK") {
      return "A planted canary token was found (proven by code). Nothing was sent.";
    }
    if (decision === "HOLD") {
      return "Held for review: possible pointer to sensitive data.";
    }
    if (decision === "REDACT") {
      var n = 0;
      if (body && body.sent_text) {
        var found = body.sent_text.match(/\[[^\]]*\]/g);
        n = found ? found.length : 0;
      }
      return "Sent with " + n + " redaction" + (n === 1 ? "" : "s") + ".";
    }
    return "Held for review: possible pointer to sensitive data.";
  }

  var KIND_ICONS = { text: "T", markdown: "M", csv: "#", docx: "W", pdf: "P", unknown: "?" };

  function renderChips() {
    if (!fileChips) return;
    fileChips.innerHTML = "";
    attachedFiles.forEach(function (f, index) {
      var chip = document.createElement("div");
      chip.className = "file-chip";
      chip.setAttribute("data-testid", "file-chip");
      chip.setAttribute("data-status", f.status);
      var icon = document.createElement("span");
      icon.className = "chip-icon";
      icon.textContent = KIND_ICONS[f.kind] || "•";
      var name = document.createElement("button");
      name.type = "button";
      name.className = "chip-name";
      name.textContent = f.name + " · " + (f.summary_text || f.status);
      name.addEventListener("click", function () {
        details.hidden = !details.hidden;
      });
      var remove = document.createElement("button");
      remove.type = "button";
      remove.className = "chip-x";
      remove.setAttribute("aria-label", "Remove " + f.name);
      remove.textContent = "x";
      remove.addEventListener("click", function () {
        attachedFiles.splice(index, 1);
        renderChips();
      });
      var details = document.createElement("ul");
      details.className = "chip-findings";
      details.hidden = true;
      (f.spans || []).forEach(function (s) {
        var li = document.createElement("li");
        li.textContent = (s.cls || "issue") + " / " + (s.subclass || "") +
          " (" + (s.status || "") + ")";
        details.appendChild(li);
      });
      if (!(f.spans || []).length) {
        var empty = document.createElement("li");
        empty.className = "ev-empty";
        empty.textContent = "no findings";
        details.appendChild(empty);
      }
      chip.appendChild(icon);
      chip.appendChild(name);
      chip.appendChild(remove);
      chip.appendChild(details);
      fileChips.appendChild(chip);
    });
  }

  if (paperclip && fileInput) {
    paperclip.addEventListener("click", function () { fileInput.click(); });
    fileInput.addEventListener("change", function () {
      var file = fileInput.files && fileInput.files[0];
      if (!file) return;
      var form = new FormData();
      form.append("file", file, file.name);
      fetch("/api/upload", { method: "POST", body: form })
        .then(function (resp) {
          return resp.json().then(function (body) {
            return { ok: resp.ok, status: resp.status, body: body };
          });
        })
        .then(function (res) {
          if (res.ok) {
            attachedFiles.push(res.body);
            renderChips();
          } else {
            var err = document.createElement("div");
            err.className = "banner";
            err.setAttribute("data-testid", "banner");
            err.setAttribute("data-decision", "HOLD");
            err.textContent = "Could not attach " + file.name +
              ": " + ((res.body && res.body.detail && res.body.detail.message) ||
                ("unsupported file (" + res.status + ")"));
            thread.appendChild(err);
            updateEmptyHint();
          }
          fileInput.value = "";
        })
        .catch(function () { fileInput.value = ""; });
    });
  }

  function refreshStats() {
    fetch("/api/stats")
      .then(function (resp) { return resp.json(); })
      .then(function (stats) {
        var decisions = stats.decisions || {};
        var box = document.getElementById("decision-counts");
        box.innerHTML = "";
        ["ALLOW", "REDACT", "BLOCK", "HOLD"].forEach(function (name) {
          var row = document.createElement("div");
          var label = document.createElement("span");
          label.textContent = name;
          var value = document.createElement("b");
          value.textContent = String(decisions[name] || 0);
          row.appendChild(label);
          row.appendChild(document.createTextNode(" "));
          row.appendChild(value);
          box.appendChild(row);
        });
        var byTier = stats.by_tier || {};
        var total = stats.total || 0;
        var bar = document.getElementById("tier-bar");
        bar.innerHTML = "";
        ["1", "2", "3"].forEach(function (tier, i) {
          var count = byTier[tier] || 0;
          var seg = document.createElement("span");
          seg.className = "t" + (i + 1);
          seg.style.width = total ? (100 * count / total) + "%" : "0%";
          seg.title = "tier " + tier + ": " + count;
          bar.appendChild(seg);
        });
        var last = stats.last_latency_ms || {};
        var lastVal = last["1"] != null ? last["1"] : last["2"];
        document.getElementById("last-latency").textContent =
          lastVal != null ? Number(lastVal).toFixed(1) + " ms" : "—";
        document.getElementById("stats-tier1").textContent = String(byTier["1"] || 0);
        document.getElementById("stats-holds").textContent = String(decisions.HOLD || 0);
        document.getElementById("stats-held-resolved").textContent =
          String(stats.held_then_resolved || 0);
      })
      .catch(function () { /* stats are advisory */ });
    fetch("/api/log/verify")
      .then(function (resp) { return resp.json(); })
      .then(function (result) {
        var badge = document.getElementById("log-badge");
        var n = result.events || 0;
        if (result.ok) {
          badge.classList.remove("broken");
          badge.textContent = "log chain verified · " + n + " event" + (n === 1 ? "" : "s");
        } else {
          badge.classList.add("broken");
          badge.textContent = "log chain broken · " + n + " events";
        }
      })
      .catch(function () { /* badge keeps its last state */ });
  }

  function clearComposer() {
    composer.value = "";
    spans = [];
    choices = {};
    renderBackdrop("", []);
    renderIssues([], null);
    syncScroll();
  }

  function clearAttachments() {
    attachedFiles = [];
    renderChips();
  }

  function sendNow() {
    var text = composer.value;
    if (!text && !attachedFiles.length) return;
    // Only spans the user explicitly clicked Redact/Keep on are sent;
    // anything untouched redacts by default without a recorded choice.
    var choiceList = spans
      .filter(function (s) {
        return Object.prototype.hasOwnProperty.call(choices, s.span_id);
      })
      .map(function (s) {
        return { span_id: s.span_id, choice: choices[s.span_id] };
      });
    var userBubble = document.createElement("div");
    userBubble.className = "bubble user";
    userBubble.textContent = text;
    thread.appendChild(userBubble);
    noteUserMessage(text);
    setMembrane("inspecting");
    checking.hidden = false;
    sendBtn.disabled = true;

    fetch("/api/send", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        messages: [{ role: "user", content: text }],
        choices: choiceList,
        file_ids: attachedFiles.map(function (f) { return f.file_id; }),
        provider: providerSel ? providerSel.value : "ollama"
      })
    })
      .then(function (resp) { return resp.json(); })
      .then(function (body) {
        if (body.provider) setPill(body.provider);
        var decision = body.decision || "HOLD";
        // One compact receipt per turn (keeps the legacy banner class so
        // recorders and trace pairing still find it). No full-width banner.
        var banner = document.createElement("details");
        banner.className = "receipt banner";
        banner.setAttribute("data-testid", "receipt");
        banner.setAttribute("data-decision", decision);
        // Plain user-facing line only; machine reasons (span ids, hashes)
        // stay in the log and are never rendered here.
        if (body && typeof body.doc_warning === "string" && body.doc_warning) {
          var docWarning = document.createElement("div");
          docWarning.className = "doc-warning";
          docWarning.setAttribute("data-testid", "doc-warning");
          docWarning.textContent = body.doc_warning;
          thread.appendChild(docWarning);
        }
        thread.appendChild(banner);
        renderReceipt(decision, body, banner);
        noteTurn(decision, body);
        setMembrane(membraneFor(decision));

        var toggle = document.createElement("button");
        toggle.type = "button";
        toggle.className = "ai-toggle";
        toggle.setAttribute("data-testid", "ai-saw-toggle");
        toggle.textContent = "What the AI saw";
        var aiSaw = document.createElement("div");
        aiSaw.className = "ai-saw";
        aiSaw.setAttribute("data-testid", "ai-saw");
        aiSaw.hidden = true;
        if (body.sent_text != null) {
          aiSaw.innerHTML = highlightPlaceholders(body.sent_text);
        } else {
          aiSaw.textContent = "(nothing sent yet)";
        }
        toggle.addEventListener("click", function () {
          aiSaw.hidden = !aiSaw.hidden;
        });
        thread.appendChild(toggle);
        thread.appendChild(aiSaw);

        if (body.answer != null) {
          var answer = document.createElement("div");
          answer.className = "bubble answer";
          answer.setAttribute("data-testid", "answer");
          answer.innerHTML = highlightRanges(body.answer, body.answer_highlights);
          thread.appendChild(answer);
          var src = document.createElement("div");
          src.className = "answer-src";
          src.textContent = "via " + providerLabel(body.provider || (providerSel && providerSel.value));
          thread.appendChild(src);
        }
        if (decision === "HOLD" && body.decision_id) {
          renderHoldCard(body.decision_id, banner);
        }
        updateEmptyHint();
        refreshStats();
      })
      .catch(function () {
        var banner = document.createElement("details");
        banner.className = "receipt banner";
        banner.setAttribute("data-testid", "receipt");
        banner.setAttribute("data-decision", "UNKNOWN");
        thread.appendChild(banner);
        renderReceipt("UNKNOWN", null, banner);
        noteTurn("UNKNOWN", null);
        setMembrane("unknown");
        updateEmptyHint();
      })
      .then(function () {
        clearComposer();
        clearAttachments();
        checking.hidden = true;
        sendBtn.disabled = false;
      });
  }

  // Evidence card for one HOLD: two columns (proven vs suspicion),
  // a reviewing spinner with elapsed seconds, then the final outcome.
  function renderHoldCard(holdId, banner) {
    var card = document.createElement("div");
    card.className = "evidence-card";
    card.setAttribute("data-testid", "evidence-card");

    var status = document.createElement("div");
    status.className = "ev-status";
    var spinner = document.createElement("span");
    spinner.className = "spinner";
    var label = document.createElement("span");
    label.className = "ev-label";
    label.textContent = "Local Muse Glimmer (30B, on this PC) is reviewing\u2026 ";
    var elapsed = document.createElement("span");
    elapsed.className = "ev-elapsed";
    elapsed.textContent = "0s";
    status.appendChild(spinner);
    status.appendChild(label);
    status.appendChild(elapsed);
    card.appendChild(status);

    var cols = document.createElement("div");
    cols.className = "ev-cols";
    function makeCol(title) {
      var col = document.createElement("div");
      col.className = "ev-col";
      var head = document.createElement("h4");
      head.textContent = title;
      var list = document.createElement("ul");
      list.className = "ev-list";
      col.appendChild(head);
      col.appendChild(list);
      cols.appendChild(col);
      return list;
    }
    var provenList = makeCol("Proven by code");
    var suspList = makeCol("Model suspicion only");
    card.appendChild(cols);

    var previewWrap = document.createElement("div");
    previewWrap.className = "ev-preview";
    var previewHead = document.createElement("h4");
    previewHead.textContent = "Redacted preview";
    var preview = document.createElement("div");
    preview.className = "ev-preview-text";
    previewWrap.appendChild(previewHead);
    previewWrap.appendChild(preview);
    card.appendChild(previewWrap);

    var actions = document.createElement("div");
    actions.className = "ev-actions";
    card.appendChild(actions);

    thread.appendChild(card);

    var startedAt = Date.now();
    var polls = 0;
    var timer = setInterval(function () {
      elapsed.textContent = Math.floor((Date.now() - startedAt) / 1000) + "s";
    }, 1000);

    // Redacted preview with suspicion spans in purple and proven
    // spans in red/orange. Marks use UTF-16 offsets, exactly how JS
    // strings index, so plain slice() aligns with the backend marks.
    function renderPreview(el, text, marks) {
      var ordered = (marks || []).slice().sort(function (a, b) {
        return a.start - b.start;
      });
      el.innerHTML = "";
      var cursor = 0;
      ordered.forEach(function (m) {
        if (m.start < cursor || m.start >= m.end) return;
        el.appendChild(document.createTextNode(text.slice(cursor, m.start)));
        var span = document.createElement("span");
        span.className = m.kind === "suspicion" ? "pv-suspicion" : "pv-proven";
        span.textContent = text.slice(m.start, m.end);
        el.appendChild(span);
        cursor = m.end;
      });
      el.appendChild(document.createTextNode(text.slice(cursor)));
    }

    function fillList(ul, items) {
      ul.innerHTML = "";
      items.forEach(function (item) {
        var li = document.createElement("li");
        li.textContent = item;
        ul.appendChild(li);
      });
      if (!items.length) {
        var empty = document.createElement("li");
        empty.className = "ev-empty";
        empty.textContent = "\u2014";
        ul.appendChild(empty);
      }
    }

    function stop() {
      clearInterval(timer);
      spinner.hidden = true;
    }

    function finishBanner(decision, text) {
      banner.setAttribute("data-decision", decision);
      var label = banner.querySelector ? banner.querySelector(".receipt-label") : null;
      if (label) {
        label.textContent = text;
      } else {
        banner.textContent = text;
      }
      var why = banner.querySelector ? banner.querySelector(".receipt-why") : null;
      if (why && text) why.textContent = text;
    }

    function showHoldAnswer(answerText, providerId) {
      var bubble = document.createElement("div");
      bubble.className = "bubble answer";
      bubble.setAttribute("data-testid", "answer");
      bubble.innerHTML = highlightRanges(answerText, []);
      thread.appendChild(bubble);
      // Footer names the provider actually used (server-echoed), never
      // just the selector value, so a mid-flight switch cannot mislabel.
      var holdSrc = document.createElement("div");
      holdSrc.className = "answer-src";
      holdSrc.textContent = "via " + providerLabel(
        providerId || (providerSel && providerSel.value));
      thread.appendChild(holdSrc);
    }

    function rule(ok, approveBtn, denyBtn) {
      approveBtn.disabled = true;
      denyBtn.disabled = true;
      fetch("/api/holds/" + encodeURIComponent(holdId) + "/rule", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ approve: ok })
      })
        .then(function (resp) {
          return resp.json().then(function (body) {
            return { ok: resp.ok, body: body };
          });
        })
        .then(function (res) {
          if (ok && res.ok) {
            finishBanner(
              res.body.decision || "REDACT",
              res.body.user_message || "Sent with redactions."
            );
            if (res.body.answer != null) showHoldAnswer(res.body.answer, res.body.provider);
            label.textContent = "Approved: the redacted text was sent.";
          } else if (!ok && res.body && res.body.user_message) {
            finishBanner("HOLD", res.body.user_message);
            label.textContent = res.body.user_message;
          } else {
            approveBtn.disabled = false;
            denyBtn.disabled = false;
            label.textContent = "Something went wrong; try again.";
          }
          stop();
          updateEmptyHint();
          refreshStats();
        })
        .catch(function () {
          approveBtn.disabled = false;
          denyBtn.disabled = false;
          label.textContent = "Something went wrong; try again.";
        });
    }

    function offerChoice() {
      var approveBtn = document.createElement("button");
      approveBtn.type = "button";
      approveBtn.setAttribute("data-testid", "btn-approve");
      approveBtn.textContent = "Send without flagged part";
      var denyBtn = document.createElement("button");
      denyBtn.type = "button";
      denyBtn.setAttribute("data-testid", "btn-deny");
      denyBtn.textContent = "Don't send";
      approveBtn.addEventListener("click", function () { rule(true, approveBtn, denyBtn); });
      denyBtn.addEventListener("click", function () { rule(false, approveBtn, denyBtn); });
      actions.appendChild(approveBtn);
      actions.appendChild(denyBtn);
    }

    function poll() {
      if (polls++ > 180) {
        stop();
        label.textContent = "Review timed out. Decide below.";
        offerChoice();
        return;
      }
      fetch("/api/holds/" + encodeURIComponent(holdId))
        .then(function (resp) { return resp.json(); })
        .then(function (body) {
          fillList(provenList, (body.proven_by_code || []).map(function (p) {
            return p.text;
          }));
          var suspicions = body.model_suspicion_only || [];
          fillList(suspList, suspicions.map(function (m) { return m.span || ""; }));
          renderPreview(preview, body.redacted_preview || "", body.preview_marks);
          var st = body.status;
          if (body.title) label.textContent = body.title;
          if (st === "reviewing") {
            setTimeout(poll, 1000);
            return;
          }
          stop();
          if (st === "redacted_sent") {
            finishBanner("REDACT", body.user_message || "Sent with redactions.");
            if (body.answer != null) showHoldAnswer(body.answer, body.provider);
            label.textContent = body.title || body.user_message || "Sent with redactions.";
          } else if (st === "released") {
            var sent = body.sent_text || "";
            finishBanner(
              sent.indexOf("[") >= 0 ? "REDACT" : "ALLOW",
              body.user_message || "Sent."
            );
            if (body.answer != null) showHoldAnswer(body.answer, body.provider);
            label.textContent = body.title || body.user_message || "Sent.";
          } else if (st === "needs_human") {
            label.textContent = body.title || "Glimmer could not clear this \u2014 your call.";
            suspicions.forEach(function (m) {
              var judgement = m.judgement || {};
              var line = document.createElement("div");
              line.className = "ev-judgement";
              line.textContent = (judgement.cls || "unknown") +
                " \u00b7 confidence " +
                (judgement.confidence != null ? judgement.confidence : "\u2014") +
                " \u00b7 " + (m.span || "\u2014");
              actions.appendChild(line);
            });
            offerChoice();
          } else if (st === "denied") {
            finishBanner("HOLD", body.user_message || "Denied: nothing was sent.");
            label.textContent = body.user_message || "Denied: nothing was sent.";
          } else if (st === "approved") {
            var out = body.sent_text || "";
            finishBanner(
              out.indexOf("[") >= 0 ? "REDACT" : "ALLOW",
              body.user_message || "Sent."
            );
            if (body.answer != null) showHoldAnswer(body.answer, body.provider);
            label.textContent = body.user_message || "Sent.";
          }
          updateEmptyHint();
          refreshStats();
        })
        .catch(function () {
          // A failed poll is retried; the spinner keeps the elapsed time.
          setTimeout(poll, 1000);
        });
    }

    poll();
  }

  composer.addEventListener("input", function () {
    setMembrane("idle");
    liveSpans = [];
    liveSeq++; // drop any in-flight live response
    renderBackdrop(composer.value, spans.filter(function () { return false; }));
    queueScan();
  });
  composer.addEventListener("scroll", syncScroll);
  sendBtn.addEventListener("click", sendNow);
  composer.addEventListener("keydown", function (event) {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) sendNow();
  });

  /* T32 layout: only the thread scrolls. New turns stick to the bottom
     unless the user scrolled up, in which case "Jump to latest" appears.
     The composer grows to ~8 lines, then scrolls internally without moving
     its position. The details drawer scrolls on its own (see style.css). */
  var jumpBtn = document.getElementById("jump-latest");
  var stickToBottom = true;
  function threadAtBottom() {
    if (!thread) return true;
    return (thread.scrollHeight - thread.scrollTop - thread.clientHeight) < 24;
  }
  function scrollThreadToBottom() {
    if (!thread) return;
    thread.scrollTop = thread.scrollHeight;
  }
  function paintJump() {
    if (!jumpBtn || !thread) return;
    jumpBtn.hidden = threadAtBottom();
  }
  if (thread) {
    thread.addEventListener("scroll", function () {
      stickToBottom = threadAtBottom();
      paintJump();
    }, { passive: true });
    if (window.MutationObserver) {
      new MutationObserver(function () {
        if (stickToBottom) scrollThreadToBottom();
        paintJump();
      }).observe(thread, { childList: true, subtree: true });
    }
  }
  if (jumpBtn) {
    jumpBtn.addEventListener("click", function () {
      stickToBottom = true;
      scrollThreadToBottom();
      paintJump();
      composer.focus();
    });
  }
  function fitComposer() {
    if (!composer) return;
    composer.style.height = "auto";
    var maxPx = Math.round(8 * 14 * 1.5 + 16);
    composer.style.height = Math.min(composer.scrollHeight, maxPx) + "px";
    if (stickToBottom) scrollThreadToBottom();
  }
  composer.addEventListener("input", fitComposer);
  fitComposer();
  paintJump();

  renderBackdrop("", []);
  updateEmptyHint();
  setPill();
  /* T32 chat shell: Classic is the single theme + memory-only sessions. */
  var THEME_KEY = "shadowfenced.theme";
  var THEME_NAMES = ["classic"];
  var themeSel = document.getElementById("theme");
  var composerBlock = document.getElementById("composer-block");

  function applyTheme(name) {
    if (THEME_NAMES.indexOf(name) < 0) name = "classic";
    document.documentElement.setAttribute("data-theme", name);
    return name;
  }

  // Only the theme name is persisted in localStorage; chats never are.
  // Classic is the only theme, so a missing picker is expected.
  try {
    applyTheme(window.localStorage.getItem(THEME_KEY) || "classic");
    window.localStorage.setItem(THEME_KEY, "classic");
  } catch (e) { applyTheme("classic"); }

  function setMembrane(state) {
    if (composerBlock) composerBlock.setAttribute("data-membrane", state);
  }

  // Sessions live in page memory only: never localStorage, never fetched.
  var HINT_HTML = '<div id="empty-hint" data-testid="empty-hint" class="thread-hint">' +
    "Type or paste anything. Sensitive text is underlined as you type, " +
    "and only clean or redacted text is sent to the AI.</div>";
  var chats = [];
  var activeChat = null;
  var chatSeq = 0;
  var chatList = document.getElementById("chat-list");
  var newChatBtn = document.getElementById("new-chat");

  function saveActive() {
    if (!activeChat) return;
    activeChat.html = thread.innerHTML;
    activeChat.draft = composer.value;
  }

  function renderChatList() {
    if (!chatList) return;
    chatList.innerHTML = "";
    chats.forEach(function (chat) {
      var item = document.createElement("li");
      var btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = chat.title;
      if (chat === activeChat) btn.setAttribute("aria-current", "true");
      btn.addEventListener("click", function () {
        saveActive();
        activeChat = chat;
        thread.innerHTML = chat.html;
        composer.value = chat.draft || "";
        emptyHint = document.getElementById("empty-hint");
        spans = [];
        choices = {};
        renderBackdrop(composer.value, []);
        renderIssues([], null);
        setMembrane("idle");
        updateEmptyHint();
        renderChatList();
      });
      item.appendChild(btn);
      chatList.appendChild(item);
    });
  }

  function ensureChat() {
    if (activeChat) return activeChat;
    chatSeq += 1;
    activeChat = {
      id: "chat-" + chatSeq,
      title: "Chat " + chatSeq,
      html: thread.innerHTML,
      draft: ""
    };
    chats.push(activeChat);
    renderChatList();
    return activeChat;
  }

  function noteUserMessage(text) {
    var chat = ensureChat();
    if (chat.titled) return;
    var first = (text || "").split("\n")[0].slice(0, 42);
    if (first) {
      chat.title = first;
      chat.titled = true;
      renderChatList();
    }
  }

  if (newChatBtn) {
    newChatBtn.addEventListener("click", function () {
      saveActive();
      chatSeq += 1;
      activeChat = {
        id: "chat-" + chatSeq,
        title: "Chat " + chatSeq,
        html: HINT_HTML,
        draft: ""
      };
      chats.push(activeChat);
      thread.innerHTML = HINT_HTML;
      emptyHint = document.getElementById("empty-hint");
      clearComposer();
      clearAttachments();
      setMembrane("idle");
      updateEmptyHint();
      renderChatList();
      composer.focus();
    });
  }

  loadLiveHintsFlag();
  /* T31 decision receipts: one compact marker per turn. */
  var KNOWN_DECISIONS = ["ALLOW", "REDACT", "BLOCK", "HOLD"];

  var RECEIPT_ICONS = { ALLOW: "○", REDACT: "◐", HOLD: "△", BLOCK: "✕", UNKNOWN: "?" };

  function receiptIcon(decision) {
    var key = KNOWN_DECISIONS.indexOf(decision) >= 0 ? decision : "UNKNOWN";
    return RECEIPT_ICONS[key] || "?";
  }

  function receiptLabel(decision, body) {
    if (decision === "ALLOW") return "Sent";
    if (decision === "REDACT") {
      var n = 0;
      if (body && body.sent_text) {
        var found = body.sent_text.match(/\[[^\]]*\]/g);
        n = found ? found.length : 0;
      }
      return n > 0 ? n + " sensitive details protected" : "Sent with redactions";
    }
    if (decision === "HOLD") return "Review required" + " — nothing sent";
    if (decision === "BLOCK") return "Blocked" + " — not sent";
    return "Not sent" + " — couldn't check";
  }

  function membraneFor(decision) {
    if (decision === "ALLOW") return "released";
    if (decision === "REDACT") return "redacted";
    if (decision === "HOLD") return "review";
    if (decision === "BLOCK") return "blocked";
    return "unknown";
  }

  // Compact per-turn receipt built only from server-response fields
  // (decision, user_message, resolved_tier, trace step names). Raw message
  // text, span values, ids and hashes never enter the receipt, its
  // attributes, or the membrane state. Unknown -> neutral, never "sent".
  // The receipt keeps the legacy "banner" hook via its class so recorders
  // and the descriptive trace pairing still find it.
  function renderReceipt(decision, body, anchor) {
    var key = KNOWN_DECISIONS.indexOf(decision) >= 0 ? decision : "UNKNOWN";
    var item = anchor && anchor.classList && anchor.classList.contains("receipt")
      ? anchor : document.createElement("details");
    item.className = "receipt banner";
    item.setAttribute("data-testid", "receipt");
    item.setAttribute("data-decision", key);
    item.innerHTML = "";
    var head = document.createElement("summary");
    head.className = "receipt-head";
    var icon = document.createElement("span");
    icon.className = "receipt-icon";
    icon.setAttribute("aria-hidden", "true");
    icon.textContent = receiptIcon(decision);
    var label = document.createElement("span");
    label.className = "receipt-label";
    label.textContent = receiptLabel(decision, body);
    var whyLink = document.createElement("span");
    whyLink.className = "receipt-why-link";
    whyLink.textContent = "Why";
    head.appendChild(icon);
    head.appendChild(label);
    head.appendChild(whyLink);
    item.appendChild(head);
    var why = document.createElement("div");
    why.className = "receipt-why";
    var bits = [];
    if (body && typeof body.user_message === "string" && body.user_message) {
      bits.push(body.user_message);
    }
    if (body && body.resolved_tier != null) {
      bits.push("resolved at tier " + body.resolved_tier);
    }
    var steps = body && body.trace ? body.trace.map(function (entry) {
      return String((entry && entry.step) || "?");
    }) : [];
    if (steps.length) bits.push("trace: " + steps.join(" > "));
    if (!bits.length) bits.push("no server confirmation yet");
    why.textContent = bits.join(" · ");
    item.appendChild(why);
    var notes = document.createElement("div");
    notes.className = "receipt-notes";
    notes.setAttribute("data-testid", "receipt-notes");
    item.appendChild(notes);
    if (item !== anchor && anchor && anchor.insertAdjacentElement) {
      anchor.insertAdjacentElement("afterend", item);
    }
    return item;
  }

  loadHostedBanner();
  loadProviders();
  refreshStats();

  /* T31b details drawer: per-turn decision, reasons, redaction summary,
     descriptive trace, model and timing; plus session counts (kept above).
     Raw values never enter: only placeholder labels from the send response,
     reason strings, and trace step names. Unknown turns stay neutral. */
  var turns = [];
  var turnSeq = 0;
  var selectedTurn = null;

  function describePlaceholders(body) {
    if (!body || typeof body.sent_text !== "string") return [];
    var found = body.sent_text.match(/\[[^\]]*\]/g);
    return found ? found : [];
  }

  function noteTurn(decision, body) {
    turnSeq += 1;
    var key = KNOWN_DECISIONS.indexOf(decision) >= 0 ? decision : "UNKNOWN";
    var entry = {
      id: turnSeq,
      decision: key,
      label: receiptLabel(decision, body),
      userMessage: body && typeof body.user_message === "string" ? body.user_message : "",
      reasons: body && body.evidence && Array.isArray(body.evidence.reasons)
        ? body.evidence.reasons.slice(0, 6) : [],
      labels: describePlaceholders(body),
      tier: body && body.resolved_tier != null ? body.resolved_tier : null,
      trace: body && Array.isArray(body.trace) ? body.trace.slice(0, 12) : [],
      provider: body && body.provider ? body.provider : (providerSel ? providerSel.value : ""),
      ms: body && body.latency_ms && body.latency_ms.total != null
        ? body.latency_ms.total : null
    };
    turns.push(entry);
    if (turns.length > 50) turns.shift();
    selectedTurn = entry;
    paintTurn(entry);
    return entry;
  }

  function paintTurn(entry) {
    var empty = document.querySelector("[data-turn-empty]");
    var facts = document.querySelector("[data-turn-facts]");
    if (!empty || !facts) return;
    if (!entry) {
      empty.hidden = false;
      facts.hidden = true;
      return;
    }
    empty.hidden = true;
    facts.hidden = false;
    var set = function (name, value) {
      var el = document.querySelector('[data-turn-' + name + ']');
      if (el) el.textContent = value;
    };
    set("decision", entry.decision);
    set("reasons", entry.reasons.length ? entry.reasons.join("; ") : "—");
    set("redactions", entry.labels.length ? entry.labels.join(" ") : "none");
    set("model", entry.provider ? providerLabel(entry.provider) : "—");
    set("timing", entry.ms != null ? Number(entry.ms).toFixed(1) + " ms" : "—");
    var wrap = document.querySelector("[data-turn-trace]");
    var list = document.querySelector("[data-turn-trace-list]");
    if (wrap && list) {
      list.innerHTML = "";
      entry.trace.forEach(function (step) {
        var li = document.createElement("li");
        li.textContent = String((step && step.step) || "?") + " · " +
          String((step && step.result) || "");
        list.appendChild(li);
      });
      wrap.hidden = !entry.trace.length;
    }
    var drawer = document.getElementById("details-drawer");
    if (drawer && !drawer.open && window.innerWidth > 1020) drawer.open = true;
  }

  document.getElementById("thread").addEventListener("click", function (event) {
    var node = event.target;
    while (node && node !== event.currentTarget) {
      if (node.classList && node.classList.contains("receipt")) break;
      node = node.parentNode;
    }
    if (!node || node === event.currentTarget) return;
    var label = node.querySelector ? node.querySelector(".receipt-label") : null;
    var text = label ? label.textContent : "";
    for (var i = turns.length - 1; i >= 0; i--) {
      if (turns[i].label === text) {
        selectedTurn = turns[i];
        paintTurn(selectedTurn);
        return;
      }
    }
    paintTurn(selectedTurn);
  });
})();
