/* Voice dictation (T39): opt-in microphone button using Meta Muse Voice.
 *
 * The button is added to #composer-tools when that element exists, and
 * otherwise next to the Attach button. The first click shows a one-time
 * consent notice; only the consent flag is stored (localStorage key
 * "sf-dictation-consent") -- audio and transcripts are never stored.
 *
 * Recording uses MediaRecorder and stops on a second click or after
 * 60 seconds. The captured audio is then decoded with
 * AudioContext.decodeAudioData, downmixed to mono and resampled to
 * 16000 Hz with an OfflineAudioContext, encoded as 16-bit PCM WAV in
 * the browser (RIFF/WAVE header, 1 channel, 16000 Hz), and uploaded as
 * "dictation.wav" (audio/wav) to /api/transcribe so the server can send
 * it straight to Muse Voice Transcribe. The transcript is inserted at
 * the cursor in the composer and an "input" event is dispatched so the
 * normal live scan runs; nothing is ever sent automatically.
 */
(function () {
  "use strict";

  var CONSENT_KEY = "sf-dictation-consent";
  var MAX_SECONDS = 60;
  var TARGET_RATE = 16000; // 16000 Hz mono output for Muse Voice Transcribe.
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

  function decodeWith(ctx, rawBuffer) {
    /* Promise form where supported, callback form (old Safari) fallback. */
    return new Promise(function (resolve, reject) {
      var result = null;
      try {
        result = ctx.decodeAudioData(
          rawBuffer,
          function (decoded) {
            resolve(decoded);
          },
          function (err) {
            reject(err || new Error("decode failed"));
          }
        );
      } catch (err) {
        reject(err);
        return;
      }
      if (result && typeof result.then === "function") {
        result.then(resolve, reject);
      }
    });
  }

  function downmixToMono16k(audioBuffer) {
    /* Downmix to mono: 1 channel at 16000 Hz via OfflineAudioContext. */
    var OfflineCtx =
      window.OfflineAudioContext || window.webkitOfflineAudioContext;
    if (!OfflineCtx) {
      return Promise.reject(new Error("no OfflineAudioContext"));
    }
    var frames = Math.max(
      1,
      Math.ceil(audioBuffer.duration * TARGET_RATE)
    );
    var offline = new OfflineCtx(1, frames, TARGET_RATE);
    var source = offline.createBufferSource();
    source.buffer = audioBuffer;
    source.connect(offline.destination);
    source.start(0);
    return offline.startRendering();
  }

  function encodeWav(monoSamples) {
    /* Encode Float32 mono samples at 16000 Hz as 16-bit PCM WAV. */
    var numChannels = 1;
    var bitsPerSample = 16;
    var blockAlign = (numChannels * bitsPerSample) >> 3;
    var byteRate = TARGET_RATE * blockAlign;
    var dataLength = monoSamples.length * 2;
    var buffer = new ArrayBuffer(44 + dataLength);
    var view = new DataView(buffer);
    function writeString(offset, text) {
      for (var i = 0; i < text.length; i++) {
        view.setUint8(offset + i, text.charCodeAt(i));
      }
    }
    writeString(0, "RIFF");
    view.setUint32(4, 36 + dataLength, true);
    writeString(8, "WAVE");
    writeString(12, "fmt ");
    view.setUint32(16, 16, true); // PCM format chunk size
    view.setUint16(20, 1, true); // PCM
    view.setUint16(22, 1, true); // 1 channel (mono)
    view.setUint32(24, 16000, true); // 16000 Hz sample rate
    view.setUint32(28, byteRate, true);
    view.setUint16(32, blockAlign, true);
    view.setUint16(34, bitsPerSample, true);
    writeString(36, "data");
    view.setUint32(40, dataLength, true);
    for (var n = 0; n < monoSamples.length; n++) {
      var sample = monoSamples[n];
      if (sample > 1) {
        sample = 1;
      } else if (sample < -1) {
        sample = -1;
      }
      view.setInt16(44 + n * 2, Math.round(sample * 32767), true);
    }
    return new Blob([buffer], { type: "audio/wav" });
  }

  function convertToWav(blob) {
    var AudioCtx = window.AudioContext || window.webkitAudioContext;
    if (!AudioCtx) {
      return Promise.reject(new Error("no AudioContext"));
    }
    return blob
      .arrayBuffer()
      .then(function (raw) {
        var ctx = new AudioCtx();
        function done(wavBlob) {
          try {
            ctx.close();
          } catch (err) {
            /* ignore */
          }
          return wavBlob;
        }
        return decodeWith(ctx, raw)
          .then(downmixToMono16k)
          .then(function (rendered) {
            return done(encodeWav(rendered.getChannelData(0)));
          })
          .then(null, function (err) {
            done(null);
            throw err;
          });
      });
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
      form.append("file", blob, "dictation.wav");
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
      if (
        !(window.AudioContext || window.webkitAudioContext) ||
        !(window.OfflineAudioContext || window.webkitOfflineAudioContext)
      ) {
        showMessage("Dictation is not supported on this browser.");
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
            button.setAttribute("aria-label", "Transcribing");
            button.textContent = "\u2026";
            convertToWav(blob).then(
              function (wavBlob) {
                upload(wavBlob, elapsed);
              },
              function () {
                showMessage(
                  "Could not read the recording on this browser."
                );
                setIdle();
              }
            );
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
