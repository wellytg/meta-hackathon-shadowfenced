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

  var scanSeq = 0;       // latest scan request id; stale responses are dropped
  var spans = [];        // spans from the latest accepted scan
  var choices = {};      // span_id -> "redact" | "keep"
  var scanTimer = null;

  function escapeHtml(text) {
    return text
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  // Underline class: red for secrets, orange for pii/business,
  // purple dotted for beacons and anything ambiguous (needs review).
  function markClass(span) {
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
      html += '<mark data-testid="' + escapeHtml(testIdFor(span)) + '"' +
        ' data-span-id="' + escapeHtml(span.span_id) + '"' +
        ' class="' + escapeHtml(markClass(span)) + '">' +
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

  function scanNow() {
    var text = composer.value;
    var mySeq = ++scanSeq;
    if (!text) {
      spans = [];
      renderBackdrop(text, spans);
      renderIssues(spans, null);
      return;
    }
    fetch("/api/scan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: text, mode: "tier1" })
    })
      .then(function (resp) { return resp.json(); })
      .then(function (body) {
        if (mySeq !== scanSeq) return; // stale response
        spans = body.spans || [];
        choices = {};
        spans.forEach(function (s) { choices[s.span_id] = "redact"; });
        renderBackdrop(text, spans);
        renderIssues(spans, body.latency_ms);
      })
      .catch(function () { /* scan is advisory; send still gates */ });
  }

  function queueScan() {
    if (scanTimer) clearTimeout(scanTimer);
    scanTimer = setTimeout(scanNow, 250);
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

  function bannerText(decision, body) {
    if (decision === "ALLOW") return "Sent clean";
    if (decision === "REDACT") {
      var n = 0;
      if (body.sent_text) {
        var found = body.sent_text.match(/\[[^\]]*\]/g);
        n = found ? found.length : 0;
      }
      return "Sent with " + n + " redaction" + (n === 1 ? "" : "s");
    }
    if (decision === "BLOCK") return "Blocked: proven by code";
    return "Held for human review";
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

  function sendNow() {
    var text = composer.value;
    if (!text) return;
    var choiceList = spans.map(function (s) {
      return { span_id: s.span_id, choice: choices[s.span_id] || "redact" };
    });
    var userBubble = document.createElement("div");
    userBubble.className = "bubble user";
    userBubble.textContent = text;
    thread.appendChild(userBubble);
    checking.hidden = false;
    sendBtn.disabled = true;

    fetch("/api/send", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        messages: [{ role: "user", content: text }],
        choices: choiceList,
        provider: providerSel ? providerSel.value : "ollama"
      })
    })
      .then(function (resp) { return resp.json(); })
      .then(function (body) {
        var decision = body.decision || "HOLD";
        var banner = document.createElement("div");
        banner.className = "banner";
        banner.setAttribute("data-testid", "banner");
        banner.setAttribute("data-decision", decision);
        banner.textContent = bannerText(decision, body);
        if ((decision === "BLOCK" || decision === "HOLD") &&
            body.evidence && body.evidence.reasons && body.evidence.reasons.length) {
          var reason = document.createElement("small");
          reason.textContent = body.evidence.reasons.join("; ");
          banner.appendChild(reason);
        }
        thread.appendChild(banner);

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
          aiSaw.textContent = "(nothing was sent upstream)";
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
        }
        refreshStats();
      })
      .catch(function () {
        var banner = document.createElement("div");
        banner.className = "banner";
        banner.setAttribute("data-testid", "banner");
        banner.setAttribute("data-decision", "HOLD");
        banner.textContent = "Held for human review";
        thread.appendChild(banner);
      })
      .then(function () {
        checking.hidden = true;
        sendBtn.disabled = false;
      });
  }

  composer.addEventListener("input", function () {
    renderBackdrop(composer.value, spans.filter(function () { return false; }));
    queueScan();
  });
  composer.addEventListener("scroll", syncScroll);
  sendBtn.addEventListener("click", sendNow);
  composer.addEventListener("keydown", function (event) {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) sendNow();
  });

  renderBackdrop("", []);
  refreshStats();
})();
