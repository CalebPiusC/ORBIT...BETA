/* ORBIT UI behavior.
 *
 * Replaces the mockup's timer-based "streaming" fake with real Server-Sent
 * Events from ChatProvider.reply_stream. Chunks arrive as they are generated
 * and are appended to the DOM immediately — the visual behavior the mockup
 * asked to keep (word/character reveal, blinking caret, caret disappears when
 * done) is preserved, but the content is genuinely streamed, not sliced on a
 * timer.
 */
(function () {
  "use strict";

  var BRAIN_SVG =
    '<span class="brain-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
    'stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path ' +
    'd="M9 4a3 3 0 0 0-3 3 3 3 0 0 0-2 2.8A3 3 0 0 0 5 15a3 3 0 0 0 3 3h1v2M15 4a3 3 0 0 1 3 3 ' +
    "3 3 0 0 1 2 2.8A3 3 0 0 1 19 15a3 3 0 0 1-3 3h-1v2M9 4a3 3 0 0 1 3 3v9a3 3 0 0 1-3 3M15 4a3 3 0 0 0-3 3v9a3 3 0 0 0 3 3\"/>" +
    "</svg></span>";

  function el(tag, cls, html) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (html != null) node.innerHTML = html;
    return node;
  }

  // --- SSE over fetch (POST) ---------------------------------------------
  function parseSSE(raw) {
    var event = "message";
    var dataLines = [];
    var lines = raw.split("\n");
    for (var i = 0; i < lines.length; i++) {
      var line = lines[i];
      if (line.indexOf("event:") === 0) event = line.slice(6).trim();
      else if (line.indexOf("data:") === 0) dataLines.push(line.slice(6));
    }
    return { event: event, data: dataLines.join("\n") };
  }

  // Reads a streamed SSE response and calls onEvent(event, data) per event.
  function postSSE(url, body, onEvent) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    }).then(function (resp) {
      if (!resp.ok) {
        return resp.text().then(function (text) {
          onEvent("error", text || "Request failed (" + resp.status + ")");
        });
      }
      var reader = resp.body.getReader();
      var decoder = new TextDecoder();
      var buffer = "";
      function pump() {
        return reader.read().then(function (result) {
          if (result.done) {
            if (buffer.length) {
              var last = parseSSE(buffer);
              if (last.event) onEvent(last.event, last.data);
            }
            return;
          }
          buffer += decoder.decode(result.value, { stream: true });
          var idx;
          while ((idx = buffer.indexOf("\n\n")) !== -1) {
            var raw = buffer.slice(0, idx);
            buffer = buffer.slice(idx + 2);
            var ev = parseSSE(raw);
            if (ev.event) onEvent(ev.event, ev.data);
          }
          return pump();
        });
      }
      return pump();
    });
  }

  // --- chat bubble helpers ------------------------------------------------
  function makeUserBubble(log, text) {
    var bubble = el("div", "chat-bubble");
    bubble.appendChild(el("div", "who", "You · just now"));
    bubble.appendChild(el("div", "body", escapeHtml(text)));
    log.appendChild(bubble);
    return bubble;
  }

  // Orbit bubble with its own mini thinking block + answer body.
  function makeOrbitBubble(log) {
    var bubble = el("div", "chat-bubble orbit");
    bubble.appendChild(el("div", "who", BRAIN_SVG + "Orbit"));
    var think = el("div", "thinking-block");
    var thinkText = el("span", "thinking-text", "");
    think.appendChild(thinkText);
    var body = el("div", "body", "");
    bubble.appendChild(think);
    bubble.appendChild(body);
    log.appendChild(bubble);
    log.scrollTop = log.scrollHeight;
    return { bubble: bubble, think: think, thinkText: thinkText, body: body };
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  // --- mode toggle (Chat / Agent) ----------------------------------------
  function wireModeToggles() {
    var buttons = document.querySelectorAll(".mode-toggle button[data-mode]");
    Array.prototype.forEach.call(buttons, function (btn) {
      btn.addEventListener("click", function () {
        var mode = btn.getAttribute("data-mode");
        fetch("/api/mode", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ mode: mode }),
        }).finally(function () {
          window.location.href = mode === "agent" ? "/task/T1" : "/";
        });
      });
    });
  }

  // --- HOME --------------------------------------------------------------
  function bootstrapHome() {
    wireModeToggles();
    var chips = document.querySelectorAll(".chip[data-goto-task]");
    Array.prototype.forEach.call(chips, function (chip) {
      chip.addEventListener("click", function () {
        window.location.href = "/task/" + chip.getAttribute("data-goto-task");
      });
    });

    var input = document.getElementById("homeInput");
    var send = document.getElementById("homeSend");
    var thread = document.getElementById("homeThread");
    if (!input || !send || !thread) return;

    function sendMessage() {
      var text = input.value.trim();
      if (!text) return;
      input.value = "";
      thread.classList.add("visible");
      makeUserBubble(thread, text);
      var orbit = el("div", "chat-bubble orbit");
      orbit.appendChild(el("div", "who", BRAIN_SVG + "Orbit"));
      var body = el("div", "body streaming", "");
      orbit.appendChild(body);
      thread.appendChild(orbit);
      thread.scrollTop = thread.scrollHeight;

      postSSE("/api/chat", { message: text }, function (event, data) {
        if (event === "answer") {
          body.textContent += data;
          thread.scrollTop = thread.scrollHeight;
        } else if (event === "done") {
          body.classList.remove("streaming");
        } else if (event === "error") {
          body.classList.remove("streaming");
          body.textContent = "⚠ " + data;
        }
      });
    }

    send.addEventListener("click", sendMessage);
    input.addEventListener("keydown", function (e) {
      if (e.key === "Enter") sendMessage();
    });
  }

  // --- TASK --------------------------------------------------------------
  function bootstrapTask(taskId) {
    wireModeToggles();
    var log = document.getElementById("taskChatLog");
    var thinkBlock = document.getElementById("thinkingBlock");
    var thinkText = document.getElementById("thinkingText");
    var input = document.getElementById("taskInput");
    var send = document.getElementById("taskSend");

    // Stream one Orbit turn. When `useTopThinking` is true, the narration goes
    // to the task's main thinking block; otherwise into the bubble's own block.
    function streamTurn(opts) {
      var useTop = !!opts.useTopThinking;
      var targetThink = useTop ? thinkText : null;
      var targetBlock = useTop ? thinkBlock : null;
      var orbit = makeOrbitBubble(log);
      if (useTop) {
        // Top thinking block is the live narration for the whole task.
        targetThink = thinkText;
        targetBlock = thinkBlock;
      }

      postSSE(
        "/api/task/" + encodeURIComponent(taskId) + "/stream",
        { message: opts.message || "", initial: !!opts.initial },
        function (event, data) {
          if (event === "thinking") {
            targetThink.textContent += data;
            log.scrollTop = log.scrollHeight;
          } else if (event === "answer") {
            if (targetBlock && !targetBlock.classList.contains("done")) {
              targetBlock.classList.add("done");
            }
            orbit.body.classList.add("streaming");
            orbit.body.textContent += data;
            log.scrollTop = log.scrollHeight;
          } else if (event === "done") {
            if (targetBlock) targetBlock.classList.add("done");
            orbit.body.classList.remove("streaming");
          } else if (event === "error") {
            if (targetBlock) targetBlock.classList.add("done");
            orbit.body.classList.remove("streaming");
            orbit.body.textContent = "⚠ " + data;
          }
        }
      );
    }

    // Kick off the opening narration/answer on load.
    streamTurn({ initial: true, useTopThinking: true });

    if (input && send) {
      function sendSteer() {
        var text = input.value.trim();
        if (!text) return;
        input.value = "";
        makeUserBubble(log, text);
        streamTurn({ message: text, useTopThinking: false });
      }
      send.addEventListener("click", sendSteer);
      input.addEventListener("keydown", function (e) {
        if (e.key === "Enter") sendSteer();
      });
    }

    // Approval gate: explicit approve, then apply/push (which refuses if not approved).
    var approveBtn = document.getElementById("approveBtn");
    var applyBtn = document.getElementById("applyBtn");
    var state = document.getElementById("approvalState");

    if (approveBtn) {
      approveBtn.addEventListener("click", function () {
        approveBtn.disabled = true;
        if (state) state.textContent = "Approving…";
        fetch("/api/task/" + encodeURIComponent(taskId) + "/approve", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ approve: true }),
        })
          .then(function (r) {
            return r.json().then(function (j) {
              return { ok: r.ok, j: j };
            });
          })
          .then(function (res) {
            if (res.ok && res.j.approved) {
              if (state) {
                state.textContent = "✓ Approved — safe to apply";
                state.classList.add("ok");
              }
              if (applyBtn) applyBtn.classList.add("approved");
            } else {
              if (state) state.textContent = "Approval rejected.";
              approveBtn.disabled = false;
            }
          })
          .catch(function () {
            if (state) state.textContent = "Approval failed.";
            approveBtn.disabled = false;
          });
      });
    }

    if (applyBtn) {
      applyBtn.addEventListener("click", function () {
        fetch("/api/task/" + encodeURIComponent(taskId) + "/apply", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({}),
        })
          .then(function (r) {
            return r.json().then(function (j) {
              return { ok: r.ok, j: j };
            });
          })
          .then(function (res) {
            if (res.ok) {
              if (state) {
                state.textContent = "✓ Changes applied.";
                state.classList.add("ok");
              }
            } else {
              if (state) state.textContent = "⛔ " + (res.j.error || "Not approved.");
            }
          });
      });
    }
  }

  window.Orbit = {
    bootstrapHome: bootstrapHome,
    bootstrapTask: bootstrapTask,
  };
})();
