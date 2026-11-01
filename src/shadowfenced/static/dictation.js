/* Voice dictation (T34): opt-in microphone button using Meta Muse Voice.
 *
 * The button is added to #composer-tools when that element exists, and
 * otherwise next to the Attach button. The first click shows a one-time
 * consent notice; only the consent flag is stored (localStorage key
 * "sf-dictation-consent") -- audio and transcripts are never stored.
 *
 * Recording uses MediaRecorder and stops on a second click or after
 * 60 seconds. The transcript is inserted at the cursor in the composer
 * and an "input" event is dispatched so the normal live scan runs;
 * nothing is ever sent automatically.
 */
(function () {
  "use strict";

  var CONSENT_KEY = "sf-dictation-consent";
  var MAX_SECONDS = 60;
  var NOTICE_TEXT =
    "Dictation uses Meta Muse Voice in the cloud. " +
    "Your voice is sent to Meta to be turned into text. " +
    "The text is then checked here before it goes to any AI.";

  function findComposer() {
    return (
      document.querySelector('[data-testid="composer"]') ||
      document.getElementById("composer")
    );
  }

  function hasConsent() {
    try {
      return window.localStorage.getItem(CONSENT_KEY) === "1";
    } catch (err) {
      return false;
    }
  }

  function grantConsent() {
    try {
      window.localStorage.setItem(CONSENT_KEY, "1");
    } catch (err) {
      /* storage unavailable: dictation still works for this page view */
    }
  }

  function formatElapsed(ms) {
    var total = Math.floor(ms / 1000);
    var minutes = Math.floor(total / 60);
    var seconds = total % 60;
    return minutes + ":" + (seconds < 10 ? "0" : "") + seconds;
  }

  function pickMimeType() {
    var candidates = ["audio/webm", "audio/ogg", "audio/wav", "audio/mp4"];
    if (
      typeof MediaRecorder === "undefined" ||
      typeof MediaRecorder.isTypeSupported !== "function"
    ) {
      return "";
    }
    for (var i = 0; i < candidates.length; i++) {
      try {
        if (MediaRecorder.isTypeSupported(candidates[i])) {
          return candidates[i];
        }
      } catch (err) {
        /* try the next candidate */
      }
    }
    return "";
  }

  function fileNameFor(mimeType) {
    if (mimeType === "audio/ogg") return "dictation.ogg";
    if (mimeType === "audio/wav") return "dictation.wav";
    if (mimeType === "audio/mp4") return "dictation.m4a";
    return "dictation.webm";
  }

  function init() {
    var composer = findComposer();
    if (!composer) return;

    var tools = document.getElementById("composer-tools");
    var paperclip = document.getElementById("paperclip");

    var button = document.createElement("button");
    button.type = "button";
    button.setAttribute("data-testid", "mic-btn");
    button.setAttribute("aria-label", "Dictate");
    button.setAttribute("aria-pressed", "false");
    button.className = "sf-mic-btn";
    button.textContent = "\uD83C\uDFA4";

    var message = document.createElement("div");
    message.className = "sf-dictation-msg";
    message.setAttribute("role", "status");
    message.hidden = true;

    function showMessage(text) {
      message.textContent = text;
      message.hidden = false;
    }

    function clearMessage() {
      message.textContent = "";
      message.hidden = true;
    }

    if (tools) {
      tools.appendChild(button);
      tools.appendChild(message);
    } else if (paperclip && paperclip.parentNode) {
      paperclip.parentNode.insertBefore(button, paperclip.nextSibling);
      paperclip.parentNode.insertBefore(message, button.nextSibling);
    } else {
      return;
    }

    var recorder = null;
    var chunks = [];
    var stream = null;
    var timer = null;
    var stopAt = 0;
    var startedAt = 0;

    function setIdle() {
      recorder = null;
      chunks = [];
      if (stream) {
        try {
          stream.getTracks().forEach(function (track) {
            track.stop();
          });
        } catch (err) {
          /* ignore */
        }
        stream = null;
      }
      if (timer) {
        window.clearInterval(timer);
        timer = null;
      }
      button.classList.remove("sf-mic-recording");
      button.setAttribute("aria-pressed", "false");
      button.setAttribute("aria-label", "Dictate");
      button.textContent = "\uD83C\uDFA4";
    }

    function insertTranscript(text) {
      composer.focus();
      var start = composer.selectionStart;
      var end = composer.selectionEnd;
      if (typeof start !== "number" || typeof end !== "number") {
        start = composer.value.length;
        end = composer.value.length;
      }
      if (typeof composer.setRangeText === "function") {
        composer.setRangeText(text, start, end, "end");
      } else {
        composer.value =
          composer.value.slice(0, start) + text + composer.value.slice(end);
      }
      /* Run the normal live scan so underlines appear before sending. */
      composer.dispatchEvent(new Event("input", { bubbles: true }));
      composer.focus();
    }

    function upload(blob, elapsedMs) {
      var form = new FormData();
      form.append("file", blob, fileNameFor(blob.type || ""));
      form.append("duration", String(Math.round(elapsedMs / 100) / 10));
      showMessage("Transcribing\u2026");
      button.disabled = true;
      button.setAttribute("aria-label", "Transcribing");
      button.textContent = "\u2026";
      return fetch("/api/transcribe", { method: "POST", body: form })
        .then(function (resp) {
          return resp.json().then(function (body) {
            return { ok: resp.ok, body: body };
          });
        })
        .then(function (result) {
          if (
            result.ok &&
            result.body &&
            typeof result.body.text === "string" &&
            result.body.text
          ) {
            clearMessage();
            insertTranscript(result.body.text);
          } else {
            var detail =
              result.body &&
              result.body.detail &&
              result.body.detail.message;
            showMessage(
              typeof detail === "string" && detail
                ? detail
                : "Dictation failed. Please try again."
            );
          }
        })
        .catch(function () {
          showMessage("Dictation failed. Please try again.");
        })
        .then(function () {
          button.disabled = false;
          setIdle();
          button.focus();
        });
    }

    function stopRecording() {
      if (timer) {
        window.clearInterval(timer);
        timer = null;
      }
      var elapsed = Date.now() - startedAt;
      var active = recorder;
      recorder = null;
      if (active) {
        try {
          if (active.state !== "inactive") {
            active.stop();
          }
        } catch (err) {
          /* onstop below still runs */
        }
      } else {
        setIdle();
      }
      return elapsed;
    }

    function startRecording() {
      clearMessage();
      if (
        typeof MediaRecorder === "undefined" ||
        !navigator.mediaDevices ||
        typeof navigator.mediaDevices.getUserMedia !== "function"
      ) {
        showMessage("No microphone found. Dictation needs a microphone.");
        return;
      }
      var mimeType = pickMimeType();
      navigator.mediaDevices
        .getUserMedia({ audio: true })
        .then(function (micStream) {
          stream = micStream;
          chunks = [];
          var options = {};
          if (mimeType) {
            options.mimeType = mimeType;
          }
          try {
            recorder = new MediaRecorder(stream, options);
          } catch (err) {
            showMessage("Could not start recording on this browser.");
            setIdle();
            return;
          }
          recorder.ondataavailable = function (event) {
            if (event.data && event.data.size > 0) {
              chunks.push(event.data);
            }
          };
          recorder.onstop = function () {
            var elapsed = Date.now() - startedAt;
            var type = (mimeType || "audio/webm").split(";")[0];
            var blob = new Blob(chunks, { type: type });
            if (stream) {
              try {
                stream.getTracks().forEach(function (track) {
                  track.stop();
                });
              } catch (err) {
                /* ignore */
              }
              stream = null;
            }
            if (timer) {
              window.clearInterval(timer);
              timer = null;
            }
            if (!blob.size) {
              showMessage("Nothing was recorded. Please try again.");
              setIdle();
              return;
            }
            upload(blob, elapsed);
          };
          startedAt = Date.now();
          stopAt = startedAt + MAX_SECONDS * 1000;
          try {
            recorder.start();
          } catch (err) {
            showMessage("Could not start recording on this browser.");
            setIdle();
            return;
          }
          button.classList.add("sf-mic-recording");
          button.setAttribute("aria-pressed", "true");
          function paintListening() {
            var label = formatElapsed(Date.now() - startedAt);
            showMessage("Listening " + label + " \u2014 press Stop to finish.");
            button.textContent = "Stop " + label;
            button.setAttribute("aria-label", "Stop dictation (" + label + ")");
          }
          timer = window.setInterval(function () {
            var remaining = stopAt - Date.now();
            if (remaining <= 0) {
              stopRecording();
              return;
            }
            paintListening();
          }, 250);
          paintListening();
        })
        .catch(function (err) {
          var name = err && err.name ? err.name : "";
          if (name === "NotAllowedError" || name === "SecurityError") {
            showMessage(
              "Microphone permission denied. Allow microphone access to dictate."
            );
          } else if (name === "NotFoundError" || name === "OverconstrainedError") {
            showMessage("No microphone found. Dictation needs a microphone.");
          } else {
            showMessage("Could not start recording. Please try again.");
          }
          setIdle();
        });
    }

    function closeNotice() {
      var notice = document.getElementById("sf-dictation-notice");
      if (notice && notice.parentNode) {
        notice.parentNode.removeChild(notice);
      }
      button.focus();
    }

    function showNotice() {
      if (document.getElementById("sf-dictation-notice")) return;
      var notice = document.createElement("div");
      notice.id = "sf-dictation-notice";
      notice.className = "sf-dictation-notice";
      notice.setAttribute("role", "dialog");
      notice.setAttribute("aria-label", "Turn on dictation");

      var text = document.createElement("p");
      text.textContent = NOTICE_TEXT;
      notice.appendChild(text);

      var actions = document.createElement("div");
      actions.className = "sf-dictation-actions";

      var enable = document.createElement("button");
      enable.type = "button";
      enable.textContent = "Turn on dictation";
      enable.addEventListener("click", function () {
        grantConsent();
        if (notice.parentNode) {
          notice.parentNode.removeChild(notice);
        }
        startRecording();
      });

      var cancel = document.createElement("button");
      cancel.type = "button";
      cancel.textContent = "Cancel";
      cancel.addEventListener("click", closeNotice);

      actions.appendChild(enable);
      actions.appendChild(cancel);
      notice.appendChild(actions);
      notice.addEventListener("keydown", function (event) {
        if (event.key === "Escape") {
          closeNotice();
        }
      });

      if (tools) {
        tools.appendChild(notice);
      } else if (button.parentNode) {
        button.parentNode.insertBefore(notice, button.nextSibling);
      }
      enable.focus();
    }

    button.addEventListener("click", function () {
      if (recorder) {
        stopRecording();
        return;
      }
      if (!hasConsent()) {
        showNotice();
        return;
      }
      startRecording();
    });
  }

  window.ShadowfencedDictation = { formatElapsed: formatElapsed };
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
