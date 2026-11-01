/* Shadowfenced re-identification (T30) banner: plain-words notice plus a
   one-click Generalise choice. No app.js changes: this file watches the
   existing /api/send responses and renders only evidence.reid (banner and
   coarsened generalised_text). Machine reasons are never rendered. */

(function () {
  "use strict";

  if (window.__shadowfencedReidInstalled) {
    return;
  }
  window.__shadowfencedReidInstalled = true;

  function issuesBox() {
    return document.getElementById("issues");
  }

  function composer() {
    return document.getElementById("composer");
  }

  function clearReid() {
    var box = issuesBox();
    if (!box) {
      return;
    }
    var old = box.querySelectorAll(".reid-banner");
    for (var i = 0; i < old.length; i++) {
      old[i].remove();
    }
  }

  function showBeforeAfter(box, generalisedText) {
    var field = composer();
    if (!field) {
      return;
    }
    var before = field.value;
    field.value = generalisedText;
    field.dispatchEvent(new Event("input", { bubbles: true }));
    var note = document.createElement("div");
    note.className = "reid-note";
    note.setAttribute("data-testid", "reid-note");
    note.textContent = "Generalised. Your real values stay on this PC.";
    box.appendChild(note);
    var undo = document.createElement("button");
    undo.type = "button";
    undo.className = "reid-undo";
    undo.textContent = "Undo";
    undo.addEventListener("click", function () {
      field.value = before;
      field.dispatchEvent(new Event("input", { bubbles: true }));
      note.remove();
      undo.remove();
    });
    box.appendChild(undo);
  }

  function renderReid(body) {
    if (!body || !body.evidence || !body.evidence.reid) {
      return;
    }
    var info = body.evidence.reid;
    if (!info.banner || body.decision === "ALLOW") {
      return;
    }
    var box = issuesBox();
    if (!box) {
      return;
    }
    clearReid();
    var banner = document.createElement("div");
    banner.className =
      "reid-banner " + (body.decision === "HOLD" ? "reid-hold" : "reid-warn");
    banner.setAttribute("data-testid", "reid-banner");
    banner.setAttribute("data-decision", body.decision);
    var text = document.createElement("span");
    text.textContent = info.banner;
    banner.appendChild(text);
    if (body.decision === "WARN" && info.generalised_text) {
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "reid-generalise";
      btn.setAttribute("data-testid", "reid-generalise");
      btn.textContent = "Generalise";
      btn.addEventListener("click", function () {
        showBeforeAfter(box, info.generalised_text);
      });
      banner.appendChild(btn);
    }
    box.appendChild(banner);
  }

  function maybeBody(response, url) {
    if (typeof url !== "string" || url.indexOf("/api/send") === -1) {
      return;
    }
    try {
      response
        .clone()
        .json()
        .then(renderReid)
        .catch(function () {
          /* Non-JSON or already consumed: nothing to render. */
        });
    } catch (err) {
      /* Cloning failed: leave the default UI untouched. */
    }
  }

  var originalFetch = window.fetch.bind(window);
  window.fetch = function (url, options) {
    return originalFetch(url, options).then(function (response) {
      maybeBody(response, typeof url === "string" ? url : url.url);
      return response;
    });
  };

  window.ShadowfencedReid = { renderReid: renderReid, clearReid: clearReid };
})();
