/* Providers panel (T35): connect a provider with a pasted API key. */
(function () {
  "use strict";

  var NOTICE = "Your key stays on this computer. " +
    "Shadowfenced only sends checked, redacted text to the provider you pick.";

  function host() {
    var panel = document.getElementById("providers-panel");
    if (panel) return { root: panel, isDialog: false };
    var btn = document.getElementById("providers-open");
    if (!btn) {
      btn = document.createElement("button");
      btn.id = "providers-open";
      btn.type = "button";
      btn.textContent = "Providers";
      document.body.appendChild(btn);
    }
    var dialog = document.getElementById("providers-panel");
    if (!dialog) {
      dialog = document.createElement("div");
      dialog.id = "providers-panel";
      dialog.className = "providers-dialog";
      dialog.hidden = true;
      document.body.appendChild(dialog);
    }
    btn.addEventListener("click", function () {
      dialog.hidden = !dialog.hidden;
      if (!dialog.hidden) load();
    });
    return { root: dialog, isDialog: true };
  }

  function statusText(p) {
    if (!p.needs_key) return "Built in — no key needed.";
    return p.connected ? "Connected." : "Not connected.";
  }

  function storageText(p) {
    if (!p.needs_key) return "";
    if (p.storage === "keyring") return "Stored in this computer's credential store.";
    return "No credential store found — stored in the local .env file.";
  }

  function setMsg(card, text) {
    var msg = card.querySelector(".providers-msg");
    if (msg) msg.textContent = text;
  }

  function postJson(url, body) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body)
    }).then(function (resp) {
      return resp.json().then(function (data) {
        return { ok: resp.ok, status: resp.status, data: data };
      });
    });
  }

  function card(p) {
    var el = document.createElement("div");
    el.className = "provider-card";
    el.setAttribute("data-provider", p.id);
    el.setAttribute("data-testid", "provider-card-" + p.id);

    var head = document.createElement("h3");
    head.textContent = p.label;
    if (p.id === "meta") {
      var badge = document.createElement("span");
      badge.className = "provider-badge";
      badge.textContent = "DEFAULT";
      head.appendChild(badge);
    }
    var pill = document.createElement("span");
    pill.className = "provider-badge " + (p.connected ? "on" : "off");
    pill.textContent = p.connected ? "Connected" : "Not connected";
    head.appendChild(pill);
    el.appendChild(head);

    var status = document.createElement("p");
    status.className = "providers-status";
    status.textContent = statusText(p);
    el.appendChild(status);

    if (p.get_key_url) {
      var link = document.createElement("a");
      link.href = p.get_key_url;
      link.target = "_blank";
      link.rel = "noopener";
      link.textContent = "Get a key";
      el.appendChild(link);
    }

    var storeNote = document.createElement("p");
    storeNote.className = "providers-storage";
    storeNote.textContent = storageText(p);
    el.appendChild(storeNote);

    var actions = document.createElement("div");
    actions.className = "providers-actions";
    var msg = document.createElement("p");
    msg.className = "providers-msg";

    var input = null;
    if (p.needs_key) {
      var row = document.createElement("div");
      row.className = "providers-keyrow";
      input = document.createElement("input");
      input.type = "password";
      input.placeholder = "Paste key";
      input.setAttribute("autocomplete", "off");
      input.spellcheck = false;
      input.setAttribute("aria-label", p.label + " API key");
      row.appendChild(input);
      el.appendChild(row);

      var save = document.createElement("button");
      save.type = "button";
      save.textContent = "Save";
      save.addEventListener("click", function () {
        if (save.disabled) return;
        save.disabled = true;
        postJson("/api/providers/" + p.id + "/key", { key: input.value })
          .then(function (res) {
            if (res.ok) {
              input.value = "";
              setMsg(el, "Saved.");
              load();
            } else {
              save.disabled = false;
              setMsg(el, errorText(res, "Could not save the key."));
            }
          })
          .catch(function () {
            save.disabled = false;
            setMsg(el, "Could not save the key.");
          });
      });
      actions.appendChild(save);
    }

    var test = document.createElement("button");
    test.type = "button";
    test.textContent = "Test connection";
    test.addEventListener("click", function () {
      if (test.disabled) return;
      test.disabled = true;
      setMsg(el, "Testing\u2026");
      postJson("/api/providers/" + p.id + "/test")
        .then(function (res) {
          test.disabled = false;
          setMsg(el, res.data && res.data.message ? res.data.message : "No answer.");
        })
        .catch(function () {
          test.disabled = false;
          setMsg(el, "Test failed.");
        });
    });
    actions.appendChild(test);

    if (p.needs_key) {
      var drop = document.createElement("button");
      drop.type = "button";
      drop.textContent = "Disconnect";
      drop.addEventListener("click", function () {
        if (drop.disabled) return;
        drop.disabled = true;
        fetch("/api/providers/" + p.id + "/key", { method: "DELETE" })
          .then(function () { setMsg(el, "Disconnected."); load(); })
          .catch(function () {
            drop.disabled = false;
            setMsg(el, "Could not disconnect.");
          });
      });
      actions.appendChild(drop);
    }

    if (p.oauth_pkce) {
      var login = document.createElement("button");
      login.type = "button";
      login.textContent = "Log in with OpenRouter";
      login.addEventListener("click", function () {
        postJson("/api/providers/openrouter/oauth/start")
          .then(function (res) {
            if (res.ok && res.data && res.data.auth_url) {
              window.open(res.data.auth_url, "_blank", "noopener");
              setMsg(el, "Finish signing in in the opened page.");
            } else {
              setMsg(el, errorText(res, "Could not start the login."));
            }
          })
          .catch(function () { setMsg(el, "Could not start the login."); });
      });
      actions.appendChild(login);
    }

    el.appendChild(actions);
    el.appendChild(msg);
    return el;
  }

  function errorText(res, fallback) {
    if (res.status === 403 && res.data && res.data.detail &&
        typeof res.data.detail.message === "string") {
      return res.data.detail.message;
    }
    if (res.data && res.data.detail && typeof res.data.detail.message === "string") {
      return res.data.detail.message;
    }
    return fallback;
  }

  var hostedMode = false;
  var HOSTED_NOTICE = "Public demo: uses the host's Muse Spark key. Run Shadowfenced on your PC to use your own keys.";
  function load() {
    fetch("/api/hosted")
      .then(function (resp) { return resp.json(); })
      .then(function (flag) {
        hostedMode = !!(flag && flag.hosted);
        renderProviders();
      })
      .catch(function () { renderProviders(); });
  }
  function renderProviders() {
    var where = host();
    fetch("/api/providers")
      .then(function (resp) { return resp.json(); })
      .then(function (body) {
        where.root.innerHTML = "";
        if (hostedMode) {
          var hostedNote = document.createElement("p");
          hostedNote.className = "providers-notice";
          hostedNote.setAttribute("data-testid", "providers-hosted-notice");
          hostedNote.textContent = HOSTED_NOTICE;
          where.root.appendChild(hostedNote);
          return;
        }
        var note = document.createElement("p");
        note.className = "providers-notice";
        note.textContent = NOTICE;
        where.root.appendChild(note);
        (body.providers || []).forEach(function (p) {
          where.root.appendChild(card(p));
        });
      })
      .catch(function () { /* panel keeps its last state */ });
  }

  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape" && event.key !== "Esc") return;
    var where = host();
    if (where && where.root && !where.root.hidden) {
      where.root.hidden = true;
      var opener = document.getElementById("providers-open");
      if (opener) opener.focus();
    }
  });
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", load);
  } else {
    load();
  }
})();
