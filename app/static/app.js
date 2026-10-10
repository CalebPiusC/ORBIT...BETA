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
  // onFinish(err) always runs once the stream ends, with a message when the
  // turn did not complete: a rejected fetch, an HTML error page where an event
  // stream was expected, or a response that produced no text at all. Without
  // these the user sees a bubble that never fills and no clue why.
  function postSSE(url, body, onEvent, onFinish) {
    var finish = onFinish || function () {};
    var sawContent = false;
    function event(name, data) {
      if (name === "answer" || name === "thinking") sawContent = true;
      onEvent(name, data);
    }
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    })
      .catch(function (err) {
        // The request never made it, or the connection died mid-stream.
        finish(
          "Could not reach " + url + " (" + ((err && err.message) || err) + ")."
        );
      })
      .then(function (resp) {
        if (!resp) return;
        if (!resp.ok) {
          return resp
            .text()
            .then(function (text) {
              finish(text || "Request failed (" + resp.status + ")");
            })
            .catch(function () {
              finish("Request failed (" + resp.status + ").");
            });
        }
        var type = resp.headers.get("Content-Type") || "";
        if (type.indexOf("text/event-stream") === -1) {
          finish(
            "The server answered with " +
              (type || "an unknown type") +
              " instead of an event stream — this is not ORBIT's streaming endpoint."
          );
          return;
        }
        if (!resp.body || !resp.body.getReader) {
          finish("This browser cannot read a streamed response body.");
          return;
        }
        var reader = resp.body.getReader();
        var decoder = new TextDecoder();
        var buffer = "";
        function pump() {
          return reader.read().then(function (result) {
            if (result.done) {
              if (buffer.length) {
                var last = parseSSE(buffer);
                if (last.event) event(last.event, last.data);
              }
              buffer = "";
              return;
            }
            buffer += decoder.decode(result.value, { stream: true });
            var idx;
            while ((idx = buffer.indexOf("\n\n")) !== -1) {
              var raw = buffer.slice(0, idx);
              buffer = buffer.slice(idx + 2);
              var ev = parseSSE(raw);
              if (ev.event) event(ev.event, ev.data);
            }
            return pump();
          });
        }
        return pump().then(function () {
          finish(
            sawContent
              ? null
              : "The model returned nothing for this turn. Check the key and model in .env, or run the real provider instead of the demo stub."
          );
        });
      })
      .catch(function (err) {
        finish("Stream failed mid-response: " + ((err && err.message) || err) + ".");
      });
  }

  // One visible line inside an Orbit bubble, used for errors and notices.
  function showLine(body, text, cls) {
    var line = body.querySelector(".notice");
    if (!line) {
      line = el("div", "notice");
      body.appendChild(line);
    }
    line.textContent = text;
    if (cls) line.classList.add(cls);
    var thread = body.closest("[id]");
    if (thread) thread.scrollTop = thread.scrollHeight;
  }

  // --- chat bubble helpers ------------------------------------------------
  // Local "HH:MM" for the time line under a bubble.
  function nowLabel() {
    var d = new Date();
    var hh = String(d.getHours());
    var mm = String(d.getMinutes());
    return (hh.length < 2 ? "0" : "") + hh + ":" + (mm.length < 2 ? "0" : "") + mm;
  }

  // Every message gets its own row: the bubble, then a faint time line. The
  // row, not the bubble, is what sits in the log, so bubbles are never merged.
  function makeRow(log, side) {
    var row = el("div", "chat-row " + side);
    var bubble = el("div", "chat-bubble " + side);
    row.appendChild(bubble);
    row.appendChild(el("div", "chat-meta", nowLabel()));
    log.appendChild(row);
    return { row: row, bubble: bubble };
  }

  // User messages: right-aligned accent bubble. No name label.
  function makeUserBubble(log, text) {
    var bubble = makeRow(log, "user").bubble;
    bubble.appendChild(el("div", "body", escapeHtml(text)));
    return bubble;
  }

  // Orbit's answer body: the brain icon sits in front of the text and is the
  // only marker of who is speaking.
  function makeOrbitBody(bubble) {
    var parts = makeBody(bubble);
    parts.body.insertAdjacentHTML("afterbegin", BRAIN_SVG);
    return parts;
  }

  // Orbit bubble with its own mini thinking block + answer body.
  function makeOrbitBubble(log) {
    var bubble = makeRow(log, "orbit").bubble;
    var think = el("div", "thinking-block");
    var thinkText = el("span", "thinking-text", "");
    think.appendChild(thinkText);
    bubble.appendChild(think);
    var parts = makeOrbitBody(bubble);
    log.scrollTop = log.scrollHeight;
    return { bubble: bubble, think: think, thinkText: thinkText, body: parts.body, reply: parts.reply };
  }

  // A body's streamed text goes into its own span so a notice appended beside it
  // is not destroyed by the next chunk (textContent assignment clears children).
  function makeBody(host) {
    var body = el("div", "body", "");
    var reply = el("span", "reply", "");
    body.appendChild(reply);
    host.appendChild(body);
    return { body: body, reply: reply };
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  // Agent-mode status lines ("Asking Chidi…") go into the task's live activity
  // feed, newest first, the same order the server renders saved activity in.
  function addActivity(text) {
    var section = document.getElementById("taskActivity");
    if (!section) return;
    var row = el("div", "activity-row");
    row.appendChild(el("span", null, escapeHtml(text)));
    row.appendChild(el("span", "time", nowLabel()));
    var heading = section.querySelector("h4");
    section.insertBefore(row, heading ? heading.nextSibling : section.firstChild);
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

    // One turn at a time: a second message during a live stream would interleave
    // two answers into one bubble, so it is refused out loud instead of silently.
    var inflight = false;

    function sendMessage() {
      var text = input.value.trim();
      if (!text) return;
      if (inflight) {
        // Leave the text in the box (nothing is lost) and say why it won't send.
        var pending = thread.querySelector(".chat-row.orbit:last-child .body");
        if (pending) showLine(pending, "Still waiting on the reply above — send this after it finishes.", "warn");
        return;
      }
      inflight = true;
      input.value = "";
      thread.classList.add("visible");
      makeUserBubble(thread, text);
      var orbit = makeRow(thread, "orbit").bubble;
      var parts = makeOrbitBody(orbit);
      var body = parts.body;
      var reply = parts.reply;
      body.classList.add("streaming");
      thread.scrollTop = thread.scrollHeight;

      postSSE(
        "/api/chat",
        { message: text },
        function (event, data) {
          if (event === "answer") {
            reply.textContent += data;
            thread.scrollTop = thread.scrollHeight;
          } else if (event === "done") {
            body.classList.remove("streaming");
          } else if (event === "error") {
            body.classList.remove("streaming");
            showLine(body, data, "error");
          }
        },
        function (err) {
          inflight = false;
          body.classList.remove("streaming");
          if (err) showLine(body, err, "error");
        }
      );
    }

    send.addEventListener("click", sendMessage);
    input.addEventListener("keydown", function (e) {
      if (e.key === "Enter") sendMessage();
    });
    if (send.tagName === "BUTTON" && send.form) {
      // A submit-capable send control must not fall back to a GET navigation.
      send.form.addEventListener("submit", function (e) {
        e.preventDefault();
        sendMessage();
      });
    }
  }

  // --- TASK --------------------------------------------------------------
  function bootstrapTask(taskId, options) {
    var opts = options || {};
    // Whether the page should ask for the task's opening update on load. The
    // server decides (see app.server.task) so a refresh is not a model call.
    var wantInitialTurn = opts.initialTurn !== false;
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
      var orbit = makeOrbitBubble(log);
      // Narration lands in the page's top block for the opening turn (that block
      // is the task's live thought) and in the bubble's own block when the user
      // steers. The previous code left the non-top case pointing at null and
      // threw on the first thinking chunk, which froze the bubble.
      var targetThink = useTop ? thinkText : orbit.thinkText;
      var targetBlock = useTop ? thinkBlock : orbit.think;
      if (!useTop && targetBlock) targetBlock.style.display = "none";

      function revealThinking() {
        if (!useTop && targetBlock) targetBlock.style.display = "";
      }

      postSSE(
        "/api/task/" + encodeURIComponent(taskId) + "/stream",
        { message: opts.message || "", initial: !!opts.initial },
        function (event, data) {
          if (event === "status") {
            addActivity(data);
          } else if (event === "thinking") {
            if (!targetThink) return;
            revealThinking();
            targetThink.textContent += data;
            log.scrollTop = log.scrollHeight;
          } else if (event === "answer") {
            if (targetBlock && !targetBlock.classList.contains("done")) {
              targetBlock.classList.add("done");
            }
            orbit.body.classList.add("streaming");
            orbit.reply.textContent += data;
            log.scrollTop = log.scrollHeight;
          } else if (event === "done") {
            if (targetBlock) targetBlock.classList.add("done");
            orbit.body.classList.remove("streaming");
          } else if (event === "error") {
            if (targetBlock) targetBlock.classList.add("done");
            orbit.body.classList.remove("streaming");
            showLine(orbit.body, data, "error");
          }
        },
        function (err) {
          if (targetBlock) targetBlock.classList.add("done");
          orbit.body.classList.remove("streaming");
          if (err) showLine(orbit.body, err, "error");
        }
      );
    }

    if (wantInitialTurn) {
      streamTurn({ initial: true, useTopThinking: true });
    } else if (thinkBlock) {
      // Nothing is streaming into the top block, so stop its "thinking" caret
      // rather than leaving a blinking empty box under the header.
      thinkBlock.classList.add("done");
    }

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
