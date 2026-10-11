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
    var thinkLabel = el("span", "thinking-label", "");
    thinkLabel.hidden = true;
    think.appendChild(thinkLabel);
    var thinkText = el("span", "thinking-text", "");
    think.appendChild(thinkText);
    bubble.appendChild(think);
    var parts = makeOrbitBody(bubble);
    log.scrollTop = log.scrollHeight;
    return {
      bubble: bubble,
      think: think,
      thinkText: thinkText,
      thinkLabel: thinkLabel,
      body: parts.body,
      reply: parts.reply,
    };
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

  // Agent-mode status lines ("Asking Titan…", or "Titan didn't answer … Asking
  // Europa…") label the narration with the model that is being asked. Only the
  // trailing "Asking <name>…" is shown as the label; the full line goes to the
  // activity feed.
  var ASKING_RE = /Asking [A-Za-z]+\u2026\s*$/;
  function showModelLabel(labelNode, text) {
    if (!labelNode) return;
    var match = ASKING_RE.exec(text || "");
    if (!match) return;
    labelNode.textContent = match[0].trim();
    labelNode.hidden = false;
  }

  // Agent-mode status lines go into the task's live activity feed, newest first,
  // the same order the server renders saved activity in.
  function addActivity(text) {
    var section = document.getElementById("taskActivity");
    if (!section) return;
    var row = el("div", "activity-row");
    row.appendChild(el("span", null, escapeHtml(text)));
    row.appendChild(el("span", "time", nowLabel()));
    var heading = section.querySelector("h4");
    section.insertBefore(row, heading ? heading.nextSibling : section.firstChild);
  }

  // --- sidebar history ----------------------------------------------------
  // "+ New conversation" resets the server's active conversation, then reloads
  // to a fresh, centered composer.
  // --- proposed tool actions (Chat mode) ----------------------------------
  // The card is the only place a tool runs from. Text the model supplied goes in
  // through textContent, never innerHTML.
  var ACTION_STATE_TEXT = { approved: "Approved", declined: "Declined", failed: "Failed", expired: "Expired" };

  function appendActionCard(log, card) {
    var bubble = makeRow(log, "orbit").bubble;
    var container = el("div", "action-card");
    container.setAttribute("data-action-id", card.id);
    bubble.appendChild(container);
    renderActionCard(container, card);
    log.scrollTop = log.scrollHeight;
  }

  function renderActionCard(container, card) {
    container.textContent = "";
    container.setAttribute("data-state", card.state);
    var body = el("div", "body");
    body.appendChild(el("span", "action-label", null));
    body.lastChild.textContent = "Proposed action · nothing runs until you approve";
    var summary = el("span", "action-summary", null);
    summary.textContent = card.summary;
    body.appendChild(summary);
    container.appendChild(body);
    if (card.state === "pending") {
      var bar = el("div", "action-buttons", null);
      var approve = el("button", "action-approve", null);
      approve.type = "button";
      approve.textContent = "Approve";
      var decline = el("button", "action-decline", null);
      decline.type = "button";
      decline.textContent = "Decline";
      approve.addEventListener("click", function () { decideAction(container, card, true); });
      decline.addEventListener("click", function () { decideAction(container, card, false); });
      bar.appendChild(approve);
      bar.appendChild(decline);
      container.appendChild(bar);
    } else {
      var text = ACTION_STATE_TEXT[card.state] || card.state;
      if (card.result) text += " — " + card.result;
      var result = el("div", "action-result", null);
      result.textContent = text;
      container.appendChild(result);
    }
  }

  function decideAction(container, card, approved) {
    Array.prototype.forEach.call(container.querySelectorAll("button"), function (b) { b.disabled = true; });
    fetch("/api/actions/" + encodeURIComponent(card.id) + "/" + (approved ? "approve" : "decline"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    })
      .then(function (res) {
        return res.json().catch(function () { return {}; }).then(function (data) {
          if (res.status === 404) {
            // Already answered, or the server restarted: nothing ran.
            renderActionCard(container, { id: card.id, summary: card.summary, state: "expired", result: data.error || "" });
            return;
          }
          if (!res.ok) throw new Error(data.error || ("HTTP " + res.status));
          renderActionCard(container, { id: card.id, summary: card.summary, state: data.state, result: data.result || "" });
        });
      })
      .catch(function (err) {
        // The outcome is unknown, so the buttons come back and nothing is claimed.
        Array.prototype.forEach.call(container.querySelectorAll("button"), function (b) { b.disabled = false; });
        var line = el("div", "notice warn", null);
        line.textContent = "Could not confirm that with the server, so nothing is marked done. Try again. (" + ((err && err.message) || err) + ")";
        container.appendChild(line);
      });
  }

  // Reminders: dismiss a notice card, or switch reminders off and on. The server
  // is the source of truth; the page only changes once the server agrees.
  function wireReminders() {
    var box = document.getElementById("heartbeatNotices");
    if (box) {
      Array.prototype.forEach.call(box.querySelectorAll("[data-dismiss-notice]"), function (btn) {
        btn.addEventListener("click", function () {
          var id = btn.getAttribute("data-dismiss-notice");
          btn.disabled = true;
          fetch("/api/heartbeat/notices/" + encodeURIComponent(id) + "/dismiss", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: "{}",
          })
            .then(function (res) {
              if (!res.ok) throw new Error("HTTP " + res.status);
              var card = btn.closest(".heartbeat-card");
              if (card) card.parentNode.removeChild(card);
              if (!box.querySelector(".heartbeat-card")) box.hidden = true;
            })
            .catch(function () {
              btn.disabled = false; // still on screen; the person can try again
            });
        });
      });
    }

    var toggle = document.getElementById("remindersToggle");
    if (!toggle) return;
    var state = document.getElementById("remindersState");
    function say(text, warn) {
      if (!state) return;
      state.textContent = text;
      state.className = warn ? "sub warn" : "sub";
    }
    toggle.addEventListener("click", function () {
      // aria-checked "true" means reminders are on, so the click pauses them.
      var pauseNow = toggle.getAttribute("aria-checked") === "true";
      toggle.disabled = true;
      fetch("/api/heartbeat/pause", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ paused: pauseNow }),
      })
        .then(function (res) {
          if (!res.ok) throw new Error("HTTP " + res.status);
          return res.json();
        })
        .then(function (data) {
          var paused = data.paused === true;
          toggle.setAttribute("aria-checked", paused ? "false" : "true");
          say(paused ? "Paused" : "On · quiet 22:00–08:00", false);
        })
        .catch(function () {
          say("Could not change reminders. Try again.", true);
        })
        .then(function () {
          toggle.disabled = false;
        });
    });
  }

  function wireNewConversation() {
    var btn = document.getElementById("newConvo");
    if (!btn) return;
    btn.addEventListener("click", function () {
      fetch("/api/conversations/new", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      }).finally(function () {
        window.location.href = "/";
      });
    });
  }

  function findHistoryItem(id) {
    var items = document.querySelectorAll(".history-item[data-conv-id]");
    for (var i = 0; i < items.length; i++) {
      if (items[i].getAttribute("data-conv-id") === id) return items[i];
    }
    return null;
  }

  // A new conversation appears at the top of the Today group, highlighted as
  // the active one, the same place the server puts it on the next page load.
  function addHistoryItem(id, title) {
    var groups = document.querySelectorAll(".history-group");
    var today = null;
    for (var i = 0; i < groups.length; i++) {
      var h = groups[i].querySelector("h4");
      if (h && h.textContent.trim() === "Today") today = groups[i];
    }
    if (!today) {
      today = el("div", "history-group");
      today.appendChild(el("h4", null, "Today"));
      var sidebar = document.querySelector(".sidebar");
      var search = sidebar && sidebar.querySelector(".sidebar-search");
      if (sidebar && search) sidebar.insertBefore(today, search.nextSibling);
    }
    var prev = document.querySelectorAll(".history-item.active");
    Array.prototype.forEach.call(prev, function (n) {
      n.classList.remove("active");
    });
    var item = el("div", "history-item active", escapeHtml(title));
    item.setAttribute("data-conv-id", id);
    var heading = today.querySelector("h4");
    today.insertBefore(item, heading ? heading.nextSibling : today.firstChild);
  }

  function setHistoryTitle(id, title) {
    var item = findHistoryItem(id);
    if (item) item.textContent = title;
    else addHistoryItem(id, title);
  }

  // Lift the centered composer into the fixed bottom bar on the first send. The
  // composer is measured before and after the layout change, then animated
  // from its old place with a transform (FLIP), so it visibly slides down. With
  // reduced motion it just moves.
  function dockComposer() {
    var main = document.getElementById("homeMain");
    var composer = main && main.querySelector(".home-composer");
    if (!main || !composer || main.classList.contains("is-docked")) return;
    var before = composer.getBoundingClientRect();
    main.classList.add("is-docked");
    var after = composer.getBoundingClientRect();
    var reduce =
      window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reduce) return;
    var dy = before.top - after.top;
    if (!dy) return;
    composer.style.transition = "none";
    composer.style.transform = "translateY(" + dy + "px)";
    void composer.offsetWidth; // apply the start position before animating
    composer.style.transition = "transform 380ms cubic-bezier(0.2, 0.7, 0.2, 1)";
    composer.style.transform = "";
    setTimeout(function () {
      composer.style.transition = "";
    }, 420);
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
    wireNewConversation();
    wireReminders();
    // Cards still waiting on a decision (rendered by the server on reload).
    Array.prototype.forEach.call(document.querySelectorAll(".action-card[data-state='pending']"), function (container) {
      var summary = container.querySelector(".action-summary");
      renderActionCard(container, {
        id: container.getAttribute("data-action-id"),
        summary: summary ? summary.textContent : "",
        state: "pending",
      });
    });
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
      dockComposer();
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
          } else if (event === "conversation") {
            // The first send created it: show it in history before any reply.
            var created = JSON.parse(data);
            addHistoryItem(created.id, created.title);
          } else if (event === "title") {
            // Named after the first completed exchange, not a truncation.
            var named = JSON.parse(data);
            setHistoryTitle(named.id, named.title);
          } else if (event === "action") {
            // The model asked for a tool. It is queued and waits for Approve/Decline.
            appendActionCard(thread, JSON.parse(data));
          } else if (event === "notice") {
            showLine(body, data, "warn");
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
    wireNewConversation();
    var log = document.getElementById("taskChatLog");
    var thinkBlock = document.getElementById("thinkingBlock");
    var thinkText = document.getElementById("thinkingText");
    var thinkLabel = document.getElementById("thinkingLabel");
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
      var targetLabel = useTop ? thinkLabel : orbit.thinkLabel;
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
            showModelLabel(targetLabel, data);
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
