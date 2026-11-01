/* Shadowfenced custom rules drawer + descriptive-mode trace timeline. */
(function () {
  "use strict";

  var DESCRIPTIVE_KEY = "shadowfenced.descriptive";
  var descriptiveOn = false;
  try {
    descriptiveOn = window.localStorage.getItem(DESCRIPTIVE_KEY) === "1";
  } catch (e) { /* storage unavailable; toggle stays in memory */ }

  var pendingTraces = []; // queued /api/send traces, one per banner

  // Capture /api/send traces without disturbing app.js: the original
  // response object passes through untouched; we only read a clone.
  function watchFetch() {
    if (typeof window.fetch !== "function" || window.fetch.__sfWrapped) return;
    var original = window.fetch;
    function wrapped(url, options) {
      var result = original.apply(this, arguments);
      try {
        var target = typeof url === "string" ? url : (url && url.url) || "";
        if (target.indexOf("/api/send") >= 0 && result && result.clone) {
          result.clone().json().then(function (body) {
            if (body && Array.isArray(body.trace)) {
              pendingTraces.push(body.trace);
            }
          }).catch(function () { /* trace stays missing */ });
        }
      } catch (e) { /* never break the send flow */ }
      return result;
    }
    wrapped.__sfWrapped = true;
    window.fetch = wrapped;
  }

  function renderTrace(trace) {
    var box = document.createElement("div");
    box.className = "sf-trace";
    box.setAttribute("data-testid", "sf-trace");
    var head = document.createElement("div");
    head.className = "sf-trace-head";
    head.textContent = "Decision trace (" + trace.length + " steps)";
    box.appendChild(head);
    var list = document.createElement("ol");
    list.className = "sf-trace-list";
    trace.forEach(function (entry, index) {
      var item = document.createElement("li");
      item.className = "sf-trace-step sf-result-" + String(entry.result || "");
      var num = document.createElement("span");
      num.className = "sf-step-num";
      num.textContent = String(index + 1);
      var main = document.createElement("span");
      main.className = "sf-step-main";
      var name = document.createElement("code");
      name.className = "sf-step-name";
      name.textContent = String(entry.step || "?");
      var detail = document.createElement("span");
      detail.className = "sf-step-detail";
      detail.textContent = String(entry.detail || "");
      main.appendChild(name);
      main.appendChild(detail);
      var meta = document.createElement("span");
      meta.className = "sf-step-meta";
      var pill = document.createElement("span");
      pill.className = "sf-pill";
      pill.textContent = String(entry.result || "");
      var ms = document.createElement("span");
      ms.className = "sf-ms";
      ms.textContent = (entry.ms == null ? "0" : String(entry.ms)) + " ms";
      meta.appendChild(pill);
      meta.appendChild(ms);
      item.appendChild(num);
      item.appendChild(main);
      item.appendChild(meta);
      list.appendChild(item);
    });
    box.appendChild(list);
    return box;
  }

  function watchThread() {
    var thread = document.getElementById("thread");
    if (!thread || typeof MutationObserver !== "function") return;
    var observer = new MutationObserver(function (mutations) {
      mutations.forEach(function (mutation) {
        mutation.addedNodes.forEach(function (node) {
          if (
            node.nodeType !== 1 ||
            !node.classList ||
            !node.classList.contains("banner")
          ) {
            return;
          }
          var trace = pendingTraces.shift();
          if (!trace || !trace.length || !descriptiveOn) return;
          node.insertAdjacentElement("afterend", renderTrace(trace));
        });
      });
    });
    observer.observe(thread, { childList: true });
  }

  function ruleRow(rule, removable, onRemove) {
    var item = document.createElement("li");
    item.className = "sf-rule";
    var kind = document.createElement("span");
    kind.className = "sf-rule-kind";
    kind.textContent = String(rule.kind || "");
    var value = document.createElement("code");
    value.className = "sf-rule-value";
    value.textContent = String(rule.value || "");
    var action = document.createElement("span");
    action.className = "sf-rule-action sf-action-" + String(rule.action || "");
    action.textContent = String(rule.action || "");
    item.appendChild(kind);
    item.appendChild(value);
    item.appendChild(action);
    if (rule.note) {
      var note = document.createElement("span");
      note.className = "sf-rule-note";
      note.textContent = String(rule.note);
      item.appendChild(note);
    }
    if (rule.readonly) {
      var lock = document.createElement("span");
      lock.className = "sf-readonly";
      lock.textContent = "built-in";
      item.appendChild(lock);
    }
    if (removable) {
      var remove = document.createElement("button");
      remove.type = "button";
      remove.className = "sf-remove";
      remove.textContent = "Remove";
      remove.addEventListener("click", function () { onRemove(rule); });
      item.appendChild(remove);
    }
    return item;
  }

  function buildDrawer() {
    var overlay = document.createElement("div");
    overlay.className = "sf-overlay";
    overlay.hidden = true;
    var drawer = document.createElement("aside");
    drawer.className = "sf-drawer";
    drawer.setAttribute("role", "dialog");
    drawer.setAttribute("aria-label", "Custom rules");
    drawer.setAttribute("data-testid", "sf-drawer");

    var head = document.createElement("div");
    head.className = "sf-drawer-head";
    var title = document.createElement("h2");
    title.textContent = "Rules";
    var close = document.createElement("button");
    close.type = "button";
    close.className = "sf-close";
    close.textContent = "Close";
    head.appendChild(title);
    head.appendChild(close);
    drawer.appendChild(head);

    var hint = document.createElement("p");
    hint.className = "sf-hint";
    hint.textContent =
      "Built-in rules come from policy.yaml and cannot change here. " +
      "Custom rules apply immediately and stay in git-ignored policy.local.yaml.";
    drawer.appendChild(hint);

    var baseTitle = document.createElement("h3");
    baseTitle.textContent = "Built-in";
    drawer.appendChild(baseTitle);
    var baseList = document.createElement("ul");
    baseList.className = "sf-rules";
    drawer.appendChild(baseList);

    var customTitle = document.createElement("h3");
    customTitle.textContent = "Custom";
    drawer.appendChild(customTitle);
    var customList = document.createElement("ul");
    customList.className = "sf-rules";
    customList.setAttribute("data-testid", "sf-custom-rules");
    drawer.appendChild(customList);

    var formTitle = document.createElement("h3");
    formTitle.textContent = "Add a rule";
    drawer.appendChild(formTitle);
    var form = document.createElement("form");
    form.className = "sf-form";
    var kindSel = document.createElement("select");
    kindSel.setAttribute("aria-label", "Kind");
    ["codename", "client", "pattern"].forEach(function (kind) {
      var opt = document.createElement("option");
      opt.value = kind;
      opt.textContent = kind;
      kindSel.appendChild(opt);
    });
    var valueInput = document.createElement("input");
    valueInput.type = "text";
    valueInput.placeholder = "Value or pattern";
    valueInput.setAttribute("aria-label", "Value");
    valueInput.setAttribute("data-testid", "sf-value");
    var actionSel = document.createElement("select");
    actionSel.setAttribute("aria-label", "Action");
    ["redact", "block", "review"].forEach(function (action) {
      var opt = document.createElement("option");
      opt.value = action;
      opt.textContent = action;
      actionSel.appendChild(opt);
    });
    var noteInput = document.createElement("input");
    noteInput.type = "text";
    noteInput.placeholder = "Note (optional)";
    noteInput.setAttribute("aria-label", "Note");
    var addBtn = document.createElement("button");
    addBtn.type = "submit";
    addBtn.className = "sf-add";
    addBtn.textContent = "Add";
    addBtn.setAttribute("data-testid", "sf-add");
    var error = document.createElement("p");
    error.className = "sf-error";
    error.setAttribute("data-testid", "sf-error");
    error.hidden = true;
    form.appendChild(kindSel);
    form.appendChild(valueInput);
    form.appendChild(actionSel);
    form.appendChild(noteInput);
    form.appendChild(addBtn);
    drawer.appendChild(form);
    drawer.appendChild(error);
    overlay.appendChild(drawer);
    document.body.appendChild(overlay);

    function showError(message) {
      error.textContent = message;
      error.hidden = false;
    }

    function refresh() {
      error.hidden = true;
      fetch("/api/rules").then(function (resp) {
        if (!resp.ok) throw new Error("rules unavailable (" + resp.status + ")");
        return resp.json();
      }).then(function (body) {
        baseList.innerHTML = "";
        (body.base || []).forEach(function (rule) {
          baseList.appendChild(ruleRow(rule, false, null));
        });
        customList.innerHTML = "";
        (body.custom || []).forEach(function (rule) {
          customList.appendChild(ruleRow(rule, true, function (entry) {
            fetch("/api/rules/" + encodeURIComponent(entry.id), {
              method: "DELETE"
            }).then(function (resp) {
              if (!resp.ok) throw new Error("remove failed (" + resp.status + ")");
              refresh();
            }).catch(function (err) { showError(err.message); });
          }));
        });
        if (!customList.children.length) {
          var empty = document.createElement("li");
          empty.className = "sf-empty";
          empty.textContent = "No custom rules yet.";
          customList.appendChild(empty);
        }
      }).catch(function (err) { showError(err.message); });
    }

    form.addEventListener("submit", function (event) {
      event.preventDefault();
      error.hidden = true;
      fetch("/api/rules", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          kind: kindSel.value,
          value: valueInput.value,
          action: actionSel.value,
          note: noteInput.value
        })
      }).then(function (resp) {
        if (!resp.ok) {
          return resp.json().then(function (body) {
            var detail = body && body.detail;
            var message = (detail && detail.message) || ("add failed (" + resp.status + ")");
            throw new Error(message);
          }).catch(function (err) {
            if (err instanceof Error) throw err;
            throw new Error("add failed (" + resp.status + ")");
          });
        }
        return resp.json();
      }).then(function () {
        valueInput.value = "";
        noteInput.value = "";
        refresh();
      }).catch(function (err) { showError(err.message); });
    });

    function open() {
      overlay.hidden = false;
      refresh();
    }
    function shut() { overlay.hidden = true; }
    close.addEventListener("click", shut);
    overlay.addEventListener("click", function (event) {
      if (event.target === overlay) shut();
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && !overlay.hidden) shut();
    });
    return { open: open, refresh: refresh };
  }

  function buildHeaderButtons(drawer) {
    var bar = document.querySelector(".topbar");
    if (!bar) return;
    var actions = document.createElement("div");
    actions.className = "sf-head-actions";

    var rulesBtn = document.createElement("button");
    rulesBtn.type = "button";
    rulesBtn.className = "sf-head-btn";
    rulesBtn.setAttribute("data-testid", "sf-rules-btn");
    rulesBtn.textContent = "Rules";
    rulesBtn.addEventListener("click", drawer.open);
    actions.appendChild(rulesBtn);

    var descBtn = document.createElement("button");
    descBtn.type = "button";
    descBtn.className = "sf-head-btn";
    descBtn.setAttribute("data-testid", "sf-descriptive-toggle");
    descBtn.setAttribute("aria-pressed", descriptiveOn ? "true" : "false");
    function paint() {
      descBtn.textContent = "Descriptive mode: " + (descriptiveOn ? "on" : "off");
      descBtn.setAttribute("aria-pressed", descriptiveOn ? "true" : "false");
    }
    descBtn.addEventListener("click", function () {
      descriptiveOn = !descriptiveOn;
      try {
        window.localStorage.setItem(DESCRIPTIVE_KEY, descriptiveOn ? "1" : "0");
      } catch (e) { /* in-memory only */ }
      paint();
    });
    paint();
    actions.appendChild(descBtn);
    bar.appendChild(actions);
  }

  watchFetch();
  watchThread();
  buildHeaderButtons(buildDrawer());
})();
