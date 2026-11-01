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
  var originalImages = {}; // file_id -> original data URL (this PC only, never sent)
  var redactedCache = {}; // file_id -> object URL for the full redacted PNG
  var pendingOriginals = {}; // name:size -> original data URL until the upload lands
  var sending = false;
  var sendSeq = 0; // latest send; the 2 s status timer only fires for it
  var composerError = document.getElementById("composer-error");

  // One-line composer error next to the control that failed; it
  // clears on the next action (typing, sending, attaching).
  function showComposerError(text) {
    if (!composerError) return;
    composerError.textContent = text;
    composerError.hidden = !text;
  }
  function clearComposerError() {
    showComposerError("");
  }

  // Send stays disabled while the box is empty (no text, no attachments).
  function paintSend() {
    if (!sendBtn) return;
    sendBtn.disabled = sending || (!composer.value && !attachedFiles.length);
  }

  var PROVIDER_LABELS = {
    meta: "Meta Muse Spark (cloud)",
    ollama: "Llama 3.2 3B via Ollama (local stand-in)"
  };

  function providerLabel(id) {
    return PROVIDER_LABELS[id] || String(id);
  }

  var HEADER_PILL = "Local check \u00b7 code + Muse Glimmer (30B, this PC)";
  var GUARD_FULL = "Guard: on \u00b7 checking locally (code + Muse Glimmer)";
  var GUARD_CODE_ONLY = "Guard: on \u00b7 code checks only";
  var guardDot = document.querySelector('[data-testid="guard-dot"]');
  var modelStatus = document.querySelector('[data-testid="model-status"]');

  // Live model hints are experimental and off by default: the server
  // says whether they are on (GET /api/health returns live_hints).
  var liveHintsOn = false;

  function setPill() {
    if (!headerPill) return;
    headerPill.textContent = HEADER_PILL;
  }

  function shortModelLabel(id, label) {
    if (id === "meta") return "Muse Spark \u00b7 cloud";
    return String(label || id)
      .replace(/^Meta /, "")
      .replace(/ \((cloud|local[^)]*)\)$/i, " \u00b7 $1");
  }

  function paintModelStatus() {
    if (!modelStatus || !providerSel) return;
    var opt = providerSel.options[providerSel.selectedIndex];
    var label = opt ? opt.textContent : providerSel.value;
    modelStatus.textContent = "Model: " + shortModelLabel(providerSel.value, label);
  }

  function paintGuard(glimmer) {
    if (guardDot) guardDot.setAttribute("data-state", glimmer ? "on" : "limited");
    if (!headerPill) return;
    headerPill.textContent = glimmer ? GUARD_FULL : GUARD_CODE_ONLY;
  }

  function loadGuard() {
    fetch("/api/guard")
      .then(function (resp) { return resp.json(); })
      .then(function (body) { paintGuard(!!(body && body.glimmer)); })
      .catch(function () { paintGuard(false); });
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
        paintModelStatus();
      })
      .catch(function () { /* static options stay as the fallback */ });
  }

  providerSel.addEventListener("change", function () {
    providerSel.disabled = true;
    fetch("/api/provider", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: providerSel.value })
    })
      .then(function () {
        providerSel.disabled = false;
        setPill();
        paintModelStatus();
      })
      .catch(function () { providerSel.disabled = false; });
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

  var KIND_ICONS = { text: "T", markdown: "M", csv: "#", docx: "W", pdf: "P", image: "I", unknown: "?" };

  /* T43 image redaction: thumbnail chips, a local before/after compare,
     and the opt-in consent card. Square Classic: no rounded corners. */
  function stashOriginalImage(file) {
    var name = (file && file.name) || "";
    if (!file || (!/^image\//.test(file.type || "") && !/\.(png|jpe?g)$/i.test(name))) return;
    var key = name + ":" + file.size;
    try {
      var reader = new FileReader();
      reader.onload = function () { pendingOriginals[key] = String(reader.result || ""); };
      reader.onerror = function () { pendingOriginals[key] = ""; };
      reader.readAsDataURL(file);
    } catch (e) { pendingOriginals[key] = ""; }
  }

  function showImageCompare(box, f) {
    box.innerHTML = "";
    box.hidden = false;
    var wrap = document.createElement("div");
    wrap.className = "img-compare-row";
    function cell(label, src) {
      var fig = document.createElement("figure");
      fig.className = "img-cell";
      var img = document.createElement("img");
      img.alt = label + " " + f.name;
      img.src = src;
      var cap = document.createElement("figcaption");
      cap.textContent = label;
      fig.appendChild(img);
      fig.appendChild(cap);
      return { fig: fig, img: img };
    }
    var before = cell("Before (this PC only)", originalImages[f.file_id] || "");
    var after = cell("After (redacted)", "");
    wrap.appendChild(before.fig);
    wrap.appendChild(after.fig);
    box.appendChild(wrap);
    if (redactedCache[f.file_id]) { after.img.src = redactedCache[f.file_id]; return; }
    fetch("/api/uploads/" + encodeURIComponent(f.file_id) + "/image")
      .then(function (resp) {
        if (!resp.ok) throw new Error("redacted image unavailable");
        return resp.blob();
      })
      .then(function (blob) {
        var url = URL.createObjectURL(blob);
        redactedCache[f.file_id] = url;
        after.img.src = url;
      })
      .catch(function () { after.img.alt = "Redacted image unavailable"; });
  }

  function showImageConsent(body, file) {
    var card = document.createElement("div");
    card.className = "banner";
    card.setAttribute("data-testid", "image-consent");
    var msg = document.createElement("p");
    msg.textContent = body.notice || "";
    card.appendChild(msg);
    var row = document.createElement("div");
    row.className = "consent-row";
    [["on", "Turn on"], ["local", "Keep images local"], ["off", "Don't attach"]].forEach(function (opt) {
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "consent-btn";
      btn.textContent = opt[1];
      btn.setAttribute("data-choice", opt[0]);
      btn.addEventListener("click", function () {
        fetch("/api/image-redaction/consent", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ choice: opt[0] })
        }).then(function () {
          card.remove();
          if (opt[0] === "off") return;
          try {
            var dt = new DataTransfer();
            dt.items.add(file);
            fileInput.files = dt.files;
            fileInput.dispatchEvent(new Event("change", { bubbles: true }));
          } catch (e) { /* user re-attaches manually */ }
        });
      });
      row.appendChild(btn);
    });
    card.appendChild(row);
    thread.appendChild(card);
    updateEmptyHint();
  }

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
      name.textContent = f.name + " · " + (f.summary_text || f.status) +
        (f.method_label ? " · " + f.method_label : "");
      name.addEventListener("click", function () {
        details.hidden = !details.hidden;
        var cmp = chip.querySelector(".img-compare");
        if (cmp) {
          if (cmp.hidden) { showImageCompare(cmp, f); }
          else { cmp.hidden = true; }
        }
      });
      var remove = document.createElement("button");
      remove.type = "button";
      remove.className = "chip-x";
      remove.setAttribute("aria-label", "Remove " + f.name);
      remove.textContent = "x";
      remove.addEventListener("click", function () {
        var gone = attachedFiles[index];
        if (gone && gone.file_id) { delete originalImages[gone.file_id]; delete redactedCache[gone.file_id]; }
        attachedFiles.splice(index, 1);
        renderChips();
      });
      if (f.kind === "image" && f.thumbnail) {
        var thumb = document.createElement("img");
        thumb.className = "img-thumb";
        thumb.src = f.thumbnail;
        thumb.alt = "Redacted preview of " + f.name;
        chip.appendChild(thumb);
        var compare = document.createElement("div");
        compare.className = "img-compare";
        compare.hidden = true;
        chip.appendChild(compare);
      }
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
    paintSend();
  }

  if (paperclip && fileInput) {
    paperclip.addEventListener("click", function () { fileInput.click(); });
    fileInput.addEventListener("change", function () {
      clearComposerError();
      var file = fileInput.files && fileInput.files[0];
      if (!file) return;
      stashOriginalImage(file);
      var isImage = (/^image\//.test(file.type || "")) ||
        (/\.(png|jpe?g)$/i.test(file.name || ""));
      if (isImage && checking) {
        checking.textContent = "Checking image locally…";
        checking.hidden = false;
      }
      var doneCheckingImage = function () {
        if (isImage && checking) {
          checking.textContent = "Checking locally…";
          if (!sending) checking.hidden = true;
        }
      };
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
            if (res.body && res.body.consent_required) {
              showImageConsent(res.body, file);
            } else {
              attachedFiles.push(res.body);
              var okey = file.name + ":" + file.size;
              if (res.body && res.body.file_id && pendingOriginals[okey]) {
                originalImages[res.body.file_id] = pendingOriginals[okey];
                delete pendingOriginals[okey];
              }
              renderChips();
            }
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
          doneCheckingImage();
        })
        .catch(function () { fileInput.value = ""; doneCheckingImage(); });
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

  /* T40 system status: per-component OK / Degraded / Down / Off labels
     in the details drawer, from GET /api/health/components. Text labels
     carry the meaning (never colour alone); details stay advisory. */
  function refreshSystemStatus() {
    var box = document.getElementById("system-status");
    if (!box) return;
    var labels = { ok: "OK", degraded: "Degraded", down: "Down", off: "Off" };
    fetch("/api/health/components")
      .then(function (resp) { return resp.json(); })
      .then(function (body) {
        var items = (body && body.components) || [];
        box.innerHTML = "";
        if (!items.length) {
          box.textContent = "no component data";
          return;
        }
        items.forEach(function (entry) {
          var status = (entry && entry.status) || "down";
          if (!labels[status]) status = "down";
          var row = document.createElement("div");
          row.className = "sys-row";
          var name = document.createElement("span");
          name.className = "sys-name";
          name.textContent = (entry && entry.name) || "?";
          var badge = document.createElement("span");
          badge.className = "sys-badge sys-" + status;
          badge.textContent = labels[status];
          row.appendChild(name);
          row.appendChild(badge);
          var detail = document.createElement("span");
          detail.className = "sys-detail";
          detail.textContent = (entry && entry.detail) || "";
          row.appendChild(detail);
          box.appendChild(row);
        });
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
    paintSend();
  }

  function clearAttachments() {
    attachedFiles = [];
    renderChips();
  }

  // Failure keeps the sent text: restore it when the box is empty,
  // otherwise keep the new typing and offer a one-click Restore.
  function showComposerErrorWithRestore(sentText) {
    showComposerError("Not sent — couldn't reach the checker. ");
    var restore = document.createElement("button");
    restore.type = "button";
    restore.className = "composer-restore";
    restore.setAttribute("data-testid", "composer-restore");
    restore.textContent = "Restore";
    restore.addEventListener("click", function () {
      composer.value = sentText + (composer.value ? "\n" + composer.value : "");
      composer.dispatchEvent(new Event("input", { bubbles: true }));
      composer.focus();
      clearComposerError();
    });
    composerError.appendChild(restore);
  }
  function sendNow() {
    if (sending) return; // one request in flight at a time
    var text = composer.value;
    if (!text && !attachedFiles.length) return;
    clearComposerError();
    var sentText = text;
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
    // The text is already in its bubble: clear the box now so anything
    // typed while the reply loads is kept.
    clearComposer();
    setMembrane("inspecting");
    checking.hidden = false;
    var mySend = ++sendSeq;
    setTimeout(function () {
      if (mySend === sendSeq && sending && checking && !checking.hidden) {
        checking.textContent = "Muse Glimmer is checking on this PC…";
      }
    }, 2000);
    sending = true;
    paintSend();

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
        if (body && body.evidence && body.evidence.person_search && body.evidence.person_search.banner) {
          var personHost = (banner.querySelector && banner.querySelector(".receipt-notes")) || banner;
          var personNote = document.createElement("div");
          personNote.className = "person-search-note";
          personNote.setAttribute("data-testid", "person-search-note");
          personNote.textContent = body.evidence.person_search.banner;
          personHost.appendChild(personNote);
          if (body.decision === "WARN" && body.evidence.person_search.generalised_text) {
            var personBtn = document.createElement("button");
            personBtn.type = "button";
            personBtn.className = "reid-generalise";
            personBtn.setAttribute("data-testid", "reid-generalise");
            personBtn.textContent = "Generalise";
            (function (gt) {
              personBtn.addEventListener("click", function () {
                composer.value = gt;
                composer.dispatchEvent(new Event("input", { bubbles: true }));
              });
            })(body.evidence.person_search.generalised_text);
            personHost.appendChild(personBtn);
          }
        }

        var toggle = document.createElement("button");
        toggle.type = "button";
        var riskModel = riskCardModel(body);
        if (riskModel && decision !== "HOLD") {
          var riskCard = renderRiskCard(riskModel, body, "risk-card");
          if (decision === "WARN") appendWarnChoices(riskCard, body, banner);
          thread.appendChild(riskCard);
        }
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
          renderHoldCard(body.decision_id, banner, body);
        }
        updateEmptyHint();
        refreshStats();
        // The composer was already cleared on send, so typing that landed
        // while the reply was in flight is kept as-is.
        clearAttachments();
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
        if (!composer.value && sentText) {
          composer.value = sentText;
          composer.dispatchEvent(new Event("input", { bubbles: true }));
          showComposerError("Not sent \u2014 couldn't reach the checker. " +
            "Your text was kept; try again.");
        } else if (sentText) {
          showComposerErrorWithRestore(sentText);
        } else {
          showComposerError("Not sent \u2014 couldn't reach the checker. " +
            "Try again.");
        }
      })
      .then(function () {
        checking.hidden = true;
        sending = false;
        paintSend();
      });
  }

  // Evidence card for one HOLD: two columns (proven vs suspicion),
  // a reviewing spinner with elapsed seconds, then the final outcome.
  function renderHoldCard(holdId, banner, body) {
    var holdBody = (body && typeof body === "object") ? body : { decision: "HOLD" };
    var holdModel = riskCardModel(holdBody) || riskCardModel({ decision: "HOLD" });
    var card = document.createElement("div");
    card.className = "evidence-card risk-card";
    card.setAttribute("data-testid", "evidence-card");
    card.setAttribute("data-decision", holdModel.decision);
    card.setAttribute("role", "group");
    card.setAttribute("aria-label", holdModel.title + " risk card");
    riskCardHead(card, holdModel);

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

    riskCardTail(card, holdModel);
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
      var holdGeneralised = riskGeneralisedText(holdBody);
      if (holdGeneralised) {
        var holdGen = document.createElement("button");
        holdGen.type = "button";
        holdGen.className = "reid-generalise";
        holdGen.setAttribute("data-testid", "reid-generalise");
        holdGen.textContent = "Generalise";
        holdGen.addEventListener("click", function () {
          composer.value = holdGeneralised;
          composer.dispatchEvent(new Event("input", { bubbles: true }));
          composer.focus();
        });
        actions.appendChild(holdGen);
      }
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
    clearComposerError();
    setMembrane("idle");
    liveSpans = [];
    liveSeq++; // drop any in-flight live response
    renderBackdrop(composer.value, spans.filter(function () { return false; }));
    paintSend();
    queueScan();
  });
  composer.addEventListener("scroll", syncScroll);
  sendBtn.addEventListener("click", sendNow);
  // Enter sends; Shift+Enter (or Alt+Enter) keeps the default newline.
  composer.addEventListener("keydown", function (event) {
    if (event.isComposing) return;
    if (event.key !== "Enter" || event.shiftKey || event.altKey) return;
    event.preventDefault();
    sendNow();
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
  paintModelStatus();
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
  var KNOWN_DECISIONS = ["ALLOW", "REDACT", "WARN", "HOLD", "BLOCK"];

  var RECEIPT_ICONS = { ALLOW: "○", REDACT: "◐", WARN: "△", HOLD: "△", BLOCK: "✕", UNKNOWN: "?" };

  function receiptIcon(decision) {
    var key = KNOWN_DECISIONS.indexOf(decision) >= 0 ? decision : "UNKNOWN";
    return RECEIPT_ICONS[key] || "?";
  }

  function receiptLabel(decision, body) {
    if (decision === "ALLOW") return "✓ Checked · nothing sensitive"; // T45 quiet receipt (legacy "Sent" lives on in "Sent clean" below)
    if (decision === "REDACT") {
      var n = 0;
      if (body && body.sent_text) {
        var found = body.sent_text.match(/\[[^\]]*\]/g);
        n = found ? found.length : 0;
      }
      return n > 0 ? n + " sensitive details protected" : "Sent with redactions";
    }
    if (decision === "WARN") return "Waiting for you — review";
    if (decision === "HOLD") return "Review required" + " — nothing sent";
    if (decision === "BLOCK") return "Blocked" + " — not sent";
    return "Not sent" + " — couldn't check";
  }

  function membraneFor(decision) {
    if (decision === "ALLOW") return "released";
    if (decision === "REDACT") return "redacted";
    if (decision === "WARN") return "review";
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
  function riskPlaceholderBase(placeholder) {
    var inner = String(placeholder).replace(/^\[/, "").replace(/\]$/, "");
    var cut = inner.lastIndexOf("_");
    if (cut > 0 && /^\d+$/.test(inner.slice(cut + 1))) return inner.slice(0, cut);
    return inner;
  }
  var RISK_PLACEHOLDER_LABELS = {
    SECRET: "secret", PII_EMAIL: "email address", PII_PHONE: "phone number",
    PII_ACCOUNT: "account number", PII_ROUTING: "routing number",
    PII_IBAN: "IBAN", PII_SWIFT: "SWIFT code", PII_SORT_CODE: "sort code",
    PII_HOLDER_NAME: "account holder name", PII_NAME: "name", NAME: "name",
    PII_ADDRESS: "street address", ADDRESS: "street address",
    PII_DOB: "date of birth", DOB: "date of birth",
    PII_SSN: "social security number", SSN: "social security number",
    PII_CARD: "card number", CARD: "card number",
    PII_EMPLOYEE_ID: "employee ID", EMPLOYEE_ID: "employee ID",
    PII_ACCOUNT: "account number", ACCOUNT: "account number",
    PII_ROUTING: "routing number", ROUTING: "routing number",
    PII_IBAN: "IBAN", IBAN: "IBAN",
    PII_SWIFT: "SWIFT code", SWIFT: "SWIFT code", SWIFT_CODE: "SWIFT code",
    PII_SORT_CODE: "sort code", SORT_CODE: "sort code",
    PROVIDER_KEY: "API key", API_KEY: "API key",
    PASSWORD: "password", PASSWORD_ASSIGNMENT: "password",
    ENTROPY: "possible secret",
    CONTRACT: "contract clause", CUSTOM_PATTERN: "contract clause",
    CODENAME: "project codename", CLIENT: "client name",
    FIGURE: "unreleased figure", BEACON: "possible sensitive pointer"
  };
  var RISK_REID_LABELS = {
    name: "name", dob: "date of birth", age: "age", street: "street address",
    postcode: "postcode", city: "city", employer: "employer",
    jobtitle: "job title", school: "school", gender: "gender",
    medical: "medical details", plate: "vehicle plate", vehicle: "vehicle",
    phone: "phone number", email: "email address", dated_event: "dates"
  };
  function riskLabelForBase(base) {
    if (RISK_PLACEHOLDER_LABELS[base]) return RISK_PLACEHOLDER_LABELS[base];
    return riskHumanLabel(base);
  }
  function riskArticle(word, first) {
    var an = /^[aeiou]/i.test(word || "");
    if (first) return an ? "An" : "A";
    return an ? "an" : "a";
  }
  function riskJoinLabels(labels) {
    var out = "";
    for (var i = 0; i < labels.length; i++) {
      if (i > 0) out += (i === labels.length - 1) ? " and " : ", ";
      out += riskArticle(labels[i], i === 0) + " " + labels[i];
    }
    return out;
  }
  // One human label per finding class, shared by placeholder bases and
  // span classes so the same finding never appears twice under two names.
  // Plain words only: never internal class names ("pii", "cls", ...).
  var RISK_HUMAN_LABELS = {
    "secret": "secret",
    "canary": "canary token", "canary token": "canary token",
    "provider key": "API key", "api key": "API key",
    "password": "password", "password assignment": "password",
    "entropy": "possible secret",
    "email": "email address", "email address": "email address",
    "phone": "phone number", "phone number": "phone number",
    "address": "street address", "street": "street address",
    "street address": "street address",
    "dob": "date of birth", "date of birth": "date of birth",
    "name": "name", "holder name": "account holder name",
    "account holder name": "account holder name",
    "codename": "project codename", "project codename": "project codename",
    "client": "client name", "client name": "client name",
    "custom pattern": "contract clause", "contract": "contract clause",
    "contract clause": "contract clause",
    "figure": "unreleased figure", "unreleased figure": "unreleased figure",
    "beacon": "possible sensitive pointer",
    "beacon cue": "possible sensitive pointer",
    "possible sensitive pointer": "possible sensitive pointer",
    "account": "account number", "account number": "account number",
    "routing": "routing number", "routing number": "routing number",
    "iban": "IBAN", "swift": "SWIFT code", "swift code": "SWIFT code",
    "sort code": "sort code",
    "card": "card number", "card number": "card number",
    "employee id": "employee ID",
    "ssn": "social security number",
    "social security number": "social security number"
  };
  function riskHumanLabel(raw) {
    var key = String(raw || "").toLowerCase().replace(/_/g, " ")
      .replace(/\s+/g, " ").trim();
    if (RISK_HUMAN_LABELS[key]) return RISK_HUMAN_LABELS[key];
    var short = key.replace(/^pii\s+/, "");
    if (RISK_HUMAN_LABELS[short]) return RISK_HUMAN_LABELS[short];
    if (!short || short === "pii" || short === "business" ||
        short === "beacon" || short === "secret" || short === "unknown") {
      return "sensitive detail";
    }
    return short;
  }
  function riskSpanLabel(span) {
    var cls = String((span && span.cls) || "").toLowerCase();
    var sub = String((span && span.subclass) || "").toLowerCase();
    if (sub.indexOf("canary") >= 0) return "canary token";
    var label = riskHumanLabel(sub);
    if (label === "sensitive detail" || label === "secret") {
      label = riskHumanLabel(cls);
    }
    if (label === "sensitive detail" || label === "secret") {
      label = riskHumanLabel((sub + " " + cls).trim());
    }
    return label;
  }
  function riskActionFor(decision) {
    if (decision === "REDACT") return "Replaced with placeholders";
    if (decision === "BLOCK") return "Blocked — not sent";
    if (decision === "HOLD" || decision === "WARN") {
      return "Waiting for you: Generalise · Send anyway · Cancel";
    }
    if (decision === "UNKNOWN") return "Not sent — couldn't check";
    return "Sent";
  }
  function riskCardModel(resp) {
    if (!resp || typeof resp !== "object") return null;
    var decision = String(resp.decision || "UNKNOWN").toUpperCase();
    var evidence = (resp.evidence && typeof resp.evidence === "object") ? resp.evidence : {};
    var spans = evidence.spans || resp.spans || [];
    if (!Array.isArray(spans)) spans = [];
    var placeholders = (resp.placeholders && typeof resp.placeholders === "object") ? resp.placeholders : {};
    var names = Object.keys(placeholders);
    var sending = (typeof resp.sent_text === "string") ? resp.sent_text : "";
    var i;
    if (!names.length && sending) {
      var seen = sending.match(/\[[A-Z0-9 _-]*\d[^\]]*\]/g) || [];
      for (i = 0; i < seen.length; i++) {
        if (names.indexOf(seen[i]) < 0) names.push(seen[i]);
      }
    }
    var reid = evidence.reid || {};
    var person = evidence.person_search || {};
    var combination = reid.combination || [];
    if (!Array.isArray(combination)) combination = [];
    var banner = person.banner || reid.banner || "";
    var inferred = person.inferred_labels || person.inferred || [];
    if (!Array.isArray(inferred)) inferred = [];
    var reasons = evidence.reasons || resp.reasons || [];
    if (!Array.isArray(reasons)) reasons = [];
    var liveReasons = reasons.filter(function (r) {
      return !/no spans\s*->\s*ALLOW/i.test(String(r));
    });
    var hasFindings = names.length > 0 || spans.length > 0 ||
      combination.length > 0 || !!banner || inferred.length > 0 ||
      liveReasons.length > 0 || /\[[A-Z0-9 _-]*\d[^\]]*\]/.test(sending);
    if (decision === "ALLOW" && !hasFindings) return null;
    var labels = [];
    var seenLabels = {};
    function addLabel(label) {
      if (!label || seenLabels[label]) return;
      seenLabels[label] = true;
      labels.push(label);
    }
    names.forEach(function (ph) { addLabel(riskLabelForBase(riskPlaceholderBase(ph))); });
    spans.forEach(function (s) { addLabel(riskSpanLabel(s)); });
    var why = [];
    var canary = spans.some(function (s) {
      return String((s && s.subclass) || "").toLowerCase().indexOf("canary") >= 0;
    }) || liveReasons.some(function (r) { return /canary/i.test(String(r)); });
    if (decision === "BLOCK" && canary) {
      why.push("A planted canary token was found.");
    }
    var otherLabels = labels.filter(function (l) { return l !== "canary token"; });
    var specificLabels = otherLabels.filter(function (l) {
      return l !== "secret" && l !== "sensitive detail";
    });
    if (specificLabels.length) otherLabels = specificLabels;
    if (otherLabels.length) {
      why.push(riskJoinLabels(otherLabels) + ".");
    } else if (decision === "BLOCK" && !why.length) {
      why.push("A secret that must never leave this PC was found.");
    }
    if (combination.length) {
      var comboLabels = combination.map(function (k) {
        return RISK_REID_LABELS[String(k)] || String(k).replace(/_/g, " ");
      });
      why.push("Together these identify you: " + comboLabels.join(" + ") + ".");
    } else if (banner && decision !== "BLOCK") {
      why.push(String(banner));
    }
    if (decision === "HOLD" && !why.length) {
      why.push("A possible pointer to sensitive data needs review.");
    }
    if (!why.length) {
      why.push((typeof resp.user_message === "string" && resp.user_message) ||
        "Sensitive content was found.");
    }
    var traceLines = [];
    (Array.isArray(resp.trace) ? resp.trace : []).forEach(function (entry) {
      var step = String((entry && entry.step) || "?");
      var result = entry && entry.result ? " · " + String(entry.result) : "";
      traceLines.push(step + result);
    });
    var action = riskActionFor(decision);
    return {
      decision: decision,
      title: decision,
      sending: sending,
      why: why,
      action: action,
      trace: traceLines,
      rows: [
        { id: "sending", label: "Sending", text: sending },
        { id: "why", label: "Why it's risky", text: why.join(" ") },
        { id: "action", label: "What was done", text: action }
      ]
    };
  }
  function riskCardHead(card, model) {
    var title = document.createElement("div");
    title.className = "risk-title";
    title.textContent = model.title;
    card.appendChild(title);
    var rows = document.createElement("div");
    rows.className = "risk-rows";
    model.rows.forEach(function (row) {
      var line = document.createElement("div");
      line.className = "risk-row risk-row-" + row.id;
      var name = document.createElement("span");
      name.className = "risk-label";
      name.textContent = row.label;
      var value = document.createElement("span");
      value.className = "risk-value";
      value.setAttribute("data-testid", "risk-" + row.id);
      if (row.id === "sending") {
        if (row.text) value.innerHTML = highlightPlaceholders(row.text);
        else value.textContent = "(nothing left to send)";
      } else {
        value.textContent = row.text;
      }
      line.appendChild(name);
      line.appendChild(value);
      rows.appendChild(line);
    });
    card.appendChild(rows);
  }
  function riskCardTail(card, model) {
    var toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "risk-toggle";
    toggle.setAttribute("data-testid", "risk-toggle");
    toggle.setAttribute("aria-expanded", "false");
    toggle.textContent = "Show details";
    var trace = document.createElement("div");
    trace.className = "risk-trace";
    trace.setAttribute("data-testid", "risk-trace");
    trace.hidden = true;
    if (model.trace.length) {
      var list = document.createElement("ol");
      model.trace.forEach(function (step) {
        var item = document.createElement("li");
        item.textContent = step;
        list.appendChild(item);
      });
      trace.appendChild(list);
    } else {
      trace.textContent = "No further details.";
    }
    toggle.addEventListener("click", function () {
      var open = trace.hidden;
      trace.hidden = !open;
      card.setAttribute("data-expanded", open ? "true" : "false");
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
      toggle.textContent = open ? "Hide details" : "Show details";
    });
    card.appendChild(toggle);
    card.appendChild(trace);
  }
  function renderRiskCard(model, body, testid) {
    var card = document.createElement("div");
    card.className = "risk-card";
    card.setAttribute("data-testid", testid || "risk-card");
    card.setAttribute("data-decision", model.decision);
    card.setAttribute("role", "group");
    card.setAttribute("aria-label", model.title + " risk card");
    riskCardHead(card, model);
    riskCardTail(card, model);
    return card;
  }
  function riskGeneralisedText(body) {
    if (!body || !body.evidence) return "";
    var person = body.evidence.person_search || {};
    var reid = body.evidence.reid || {};
    return person.generalised_text || reid.generalised_text || "";
  }
  function riskLockChoices(holder) {
    var btns = holder.querySelectorAll("button");
    for (var i = 0; i < btns.length; i++) btns[i].disabled = true;
  }
  function riskUnlockChoices(holder) {
    var btns = holder.querySelectorAll("button");
    for (var i = 0; i < btns.length; i++) btns[i].disabled = false;
  }
  function riskConfirmWarn(card, banner, body, url, choice) {
    var finalText = choice === "send_generalised" ?
      "Sent generalised" : "Sent anyway — you chose to send";
    fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ choice: choice })
    })
      .then(function (resp) {
        return resp.json().then(function (b) { return { ok: resp.ok, body: b }; });
      })
      .then(function (res) {
        var holder = card.querySelector(".risk-choices");
        var actionVal = card.querySelector(".risk-row-action .risk-value");
        if (res.ok) {
          if (actionVal) actionVal.textContent = finalText;
          if (holder) holder.remove();
          if (banner) {
            renderReceipt(res.body.decision || "WARN", res.body, banner);
            var receiptLabelEl = banner.querySelector ? banner.querySelector(".receipt-label") : null;
            if (receiptLabelEl) receiptLabelEl.textContent = finalText;
          }
          var oldLabel = receiptLabel(body.decision || "WARN", body);
          for (var i = turns.length - 1; i >= 0; i--) {
            if (turns[i].label === oldLabel) {
              turns[i].label = finalText;
              if (selectedTurn === turns[i]) paintTurn(turns[i]);
              break;
            }
          }
          if (res.body && res.body.answer != null) {
            var bubble = document.createElement("div");
            bubble.className = "bubble answer";
            bubble.setAttribute("data-testid", "answer");
            bubble.innerHTML = highlightRanges(res.body.answer, res.body.answer_highlights);
            thread.appendChild(bubble);
            var src = document.createElement("div");
            src.className = "answer-src";
            src.textContent = "via " + providerLabel(res.body.provider || (providerSel && providerSel.value));
            thread.appendChild(src);
          }
          refreshStats();
        } else {
          if (holder) riskUnlockChoices(holder);
          if (actionVal) actionVal.textContent = "Something went wrong; try again.";
        }
        updateEmptyHint();
      })
      .catch(function () {
        var holder = card.querySelector(".risk-choices");
        if (holder) riskUnlockChoices(holder);
        var actionVal = card.querySelector(".risk-row-action .risk-value");
        if (actionVal) actionVal.textContent = "Something went wrong; try again.";
      });
  }
  function appendWarnChoices(card, body, banner) {
    var holder = document.createElement("div");
    holder.className = "risk-choices";
    var generalised = riskGeneralisedText(body);
    var warnUrl = (body && body.confirm_url) ||
      ("/api/warns/" + encodeURIComponent((body && body.decision_id) || "") + "/confirm");
    if (generalised) {
      var gen = document.createElement("button");
      gen.type = "button";
      gen.className = "reid-generalise";
      gen.setAttribute("data-testid", "reid-generalise");
      gen.textContent = "Generalise";
      gen.addEventListener("click", function () {
        riskLockChoices(holder);
        riskConfirmWarn(card, banner, body, warnUrl, "send_generalised");
      });
      holder.appendChild(gen);
    }
    var sendBtn2 = document.createElement("button");
    sendBtn2.type = "button";
    sendBtn2.setAttribute("data-testid", "risk-send-anyway");
    sendBtn2.textContent = "Send anyway";
    var cancel = document.createElement("button");
    cancel.type = "button";
    cancel.setAttribute("data-testid", "risk-cancel");
    cancel.textContent = "Cancel";
    sendBtn2.addEventListener("click", function () {
      riskLockChoices(holder);
      riskConfirmWarn(card, banner, body, warnUrl, "send_anyway");
    });
    cancel.addEventListener("click", function () {
      riskLockChoices(holder);
      var actionVal = card.querySelector(".risk-row-action .risk-value");
      if (actionVal) actionVal.textContent = "Cancelled — nothing was sent.";
    });
    holder.appendChild(sendBtn2);
    holder.appendChild(cancel);
    var toggle = card.querySelector(".risk-toggle");
    if (toggle) card.insertBefore(holder, toggle);
    else card.appendChild(holder);
  }
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
  var extTokenValue = "";
  function paintExtToken() {
    var tokenEl = document.getElementById("ext-token");
    if (!tokenEl) return;
    var shown = tokenEl.getAttribute("data-visible") === "true";
    tokenEl.textContent = !extTokenValue ? "unavailable"
      : shown ? extTokenValue : "••••••••";
    var showBtn = document.getElementById("ext-token-show");
    if (showBtn) showBtn.textContent = shown ? "Hide" : "Show";
  }
  function tokenMsg(text, isError) {
    var el = document.getElementById("ext-token-msg");
    if (!el) return;
    el.textContent = text;
    el.hidden = !text;
    el.setAttribute("data-error", isError ? "true" : "false");
  }
  function loadExtToken() {
    var tokenEl = document.getElementById("ext-token");
    if (!tokenEl) return;
    fetch("/api/ext/pairing")
      .then(function (resp) { return resp.json(); })
      .then(function (body) {
        extTokenValue = (body && body.token) || "";
        paintExtToken();
      })
      .catch(function () {
        extTokenValue = "";
        paintExtToken();
      });
    var showBtn = document.getElementById("ext-token-show");
    if (showBtn && !showBtn.dataset.wired) {
      showBtn.dataset.wired = "1";
      showBtn.addEventListener("click", function () {
        var shown = tokenEl.getAttribute("data-visible") === "true";
        tokenEl.setAttribute("data-visible", shown ? "false" : "true");
        paintExtToken();
      });
    }
    var copyBtn = document.getElementById("ext-token-copy");
    if (copyBtn && !copyBtn.dataset.wired) {
      copyBtn.dataset.wired = "1";
      copyBtn.addEventListener("click", function () {
        if (navigator.clipboard && extTokenValue) {
          navigator.clipboard.writeText(extTokenValue).catch(function () {});
        }
      });
    }
    var regenBtn = document.getElementById("ext-token-regenerate");
    if (regenBtn && !regenBtn.dataset.wired) {
      regenBtn.dataset.wired = "1";
      regenBtn.addEventListener("click", function () {
        if (regenBtn.disabled) return;
        tokenMsg("");
        regenBtn.disabled = true;
        fetch("/api/ext/pairing/regenerate", {
          method: "POST",
          headers: { "X-Ext-Token": extTokenValue }
        })
          .then(function (resp) {
            return resp.json().then(function (body) {
              return { ok: resp.ok, body: body };
            });
          })
          .then(function (res) {
            if (res.ok && res.body && res.body.token) {
              extTokenValue = res.body.token;
              paintExtToken();
              regenBtn.disabled = false;
              tokenMsg("New token ready. Update the extension.");
            } else {
              regenBtn.disabled = false;
              tokenMsg("Could not regenerate the token.", true);
            }
          })
          .catch(function () {
            regenBtn.disabled = false;
            tokenMsg("Could not regenerate the token.", true);
          });
      });
    }
  }

  // Decision log viewer: time, outcome, tier, reasons and fingerprints
  // only (the endpoint never returns raw values); plus chain status.
  function loadLogViewer() {
    var viewBtn = document.getElementById("log-view");
    var list = document.getElementById("log-events");
    if (!viewBtn || !list || viewBtn.dataset.wired) return;
    viewBtn.dataset.wired = "1";
    viewBtn.addEventListener("click", function () {
      if (viewBtn.disabled) return;
      if (!list.hidden) {
        list.hidden = true;
        viewBtn.textContent = "View log";
        return;
      }
      viewBtn.disabled = true;
      viewBtn.textContent = "Loading\u2026";
      fetch("/api/log/events?limit=50")
        .then(function (resp) { return resp.json(); })
        .then(function (body) {
          list.innerHTML = "";
          var events = (body && body.events) || [];
          if (!events.length) {
            var empty = document.createElement("li");
            empty.textContent = body && body.ok
              ? "No decisions logged yet."
              : "Log unavailable: chain could not be verified.";
            list.appendChild(empty);
          }
          events.slice().reverse().forEach(function (entry) {
            var item = document.createElement("li");
            var bits = [
              entry.ts || "—",
              entry.decision || "—",
              "tier " + (entry.tier != null ? entry.tier : "—")
            ];
            if (entry.reasons && entry.reasons.length) {
              bits.push(entry.reasons.join(", "));
            }
            if (entry.fingerprints && entry.fingerprints.length) {
              bits.push("prints " + entry.fingerprints.join(" "));
            }
            item.textContent = bits.join(" · ");
            list.appendChild(item);
          });
          viewBtn.disabled = false;
          list.hidden = false;
          viewBtn.textContent = "Hide log";
        })
        .catch(function () {
          list.innerHTML = "";
          var failed = document.createElement("li");
          failed.textContent = "Could not load the log.";
          list.appendChild(failed);
          viewBtn.disabled = false;
          list.hidden = false;
          viewBtn.textContent = "Hide log";
        });
    });
  }

  loadProviders();
  loadExtToken();
  loadLogViewer();
  loadGuard();
  paintSend();
  refreshStats();
  refreshSystemStatus();
  /* T40: re-check components whenever the drawer is opened. */
  (function () {
    var drawer = document.getElementById("details-drawer");
    if (drawer) {
      drawer.addEventListener("toggle", function () {
        if (drawer.open) refreshSystemStatus();
      });
    }
  })();

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

  /* T38b states: one Escape closes the topmost panel first, then the
     phone panels. Panels never take the typed text with them. */
  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape" && event.key !== "Esc") return;
    var providersPanel = document.getElementById("providers-panel");
    if (providersPanel && !providersPanel.hidden) {
      providersPanel.hidden = true;
      var opener = document.getElementById("providers-open");
      if (opener) opener.focus();
      return;
    }
    var settingsMenu = document.getElementById("settings-menu");
    if (settingsMenu && settingsMenu.open) {
      settingsMenu.open = false;
      return;
    }
    if (document.body.classList.contains("show-chats")) {
      document.body.classList.remove("show-chats");
      return;
    }
    if (document.body.classList.contains("show-details")) {
      document.body.classList.remove("show-details");
      return;
    }
  });

  // Pure state helpers, exported for tests (no DOM inside).
  window.ShadowfencedUI = {
    receiptLabel: receiptLabel,
    riskCardModel: riskCardModel,
    receiptIcon: receiptIcon,
    membraneFor: membraneFor,
    isSendEnabled: function (text, hasAttachments, busy) {
      return !busy && !!(text || hasAttachments);
    }
  };
})();
