/* Shadowfenced re-identification (T30) plus person-search intent (T33).
   T50: the send-time banner is retired -- the risk card in app.js is the
   one place for send-time warnings, so this file no longer watches send
   traffic. Live typing underlines are untouched: they come from the live
   scan in app.js. renderReid/clearReid stay exported for compatibility. */

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
    if (!body || !body.evidence || (!body.evidence.reid && !body.evidence.person_search)) {
      return;
    }
    var info = body.evidence.person_search && body.evidence.person_search.banner ? {
      banner: body.evidence.person_search.banner,
      generalised_text: body.evidence.person_search.generalised_text || (body.evidence.reid && body.evidence.reid.generalised_text)
    } : body.evidence.reid;
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

  // T50: no send-flow hook. The risk card owns send-time warnings; calling
  // renderReid from fetch traffic only duplicated the banner under the
  // composer.

  window.ShadowfencedReid = { renderReid: renderReid, clearReid: clearReid };
})();
