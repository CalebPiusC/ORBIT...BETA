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

  // Reuse the same SVG asset as the wordmark and favicon for streamed replies.
  var markLink = document.querySelector('link[rel="icon"]');
  var ORBIT_MARK_SRC = markLink ? markLink.href : "/static/orbit-mark.svg";

  function orbitIcon() {
    var image = el("img", "orbit-icon");
    image.src = ORBIT_MARK_SRC;
    image.alt = "";
    image.setAttribute("aria-hidden", "true");
    return image;
  }

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
  function postSSE(url, body, onEvent, onFinish, signal) {
    var finish = onFinish || function () {};
    var sawContent = false;
    var sawErrorEvent = false;
    function event(name, data) {
      if (name === "answer" || name === "thinking") sawContent = true;
      if (name === "error") sawErrorEvent = true;
      onEvent(name, data);
    }
    var request = {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    };
    if (signal) request.signal = signal;
    return fetch(url, request)
      .catch(function (err) {
        if (signal && signal.aborted) {
          finish(null);
          return;
        }
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
            sawContent || sawErrorEvent
              ? null
              : "The model returned nothing for this turn. Check the key and model in .env, or run the real provider instead of the demo stub."
          );
        });
      })
      .catch(function (err) {
        if (signal && signal.aborted) {
          finish(null);
          return;
        }
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

  // Orbit's answer body: the orbital mark sits in front of the text and is the
  // only marker of who is speaking.
  function makeOrbitBody(bubble) {
    var parts = makeBody(bubble);
    parts.body.insertBefore(orbitIcon(), parts.reply);
    bubble.appendChild(makeMessageActions());
    return parts;
  }

  function makeMessageActions() {
    var actions = el("div", "message-actions");
    actions.setAttribute("aria-label", "Response actions");
    [
      ["copy", "Copy response", "Copy", "⧉"],
      ["regenerate", "Regenerate response", "Regenerate", "↻"],
      ["feedback-up", "Good response", "Good response", "↑"],
      ["feedback-down", "Poor response", "Poor response", "↓"],
    ].forEach(function (item) {
      var button = el("button", "message-action", item[3]);
      button.type = "button";
      button.setAttribute("data-message-action", item[0]);
      button.setAttribute("aria-label", item[1]);
      button.setAttribute("title", item[2]);
      if (item[0].indexOf("feedback-") === 0) button.setAttribute("aria-pressed", "false");
      actions.appendChild(button);
    });
    return actions;
  }

  function messageText(row) {
    var reply = row && row.querySelector(".reply");
    return reply ? reply.textContent : "";
  }

  function copyMessage(button, row) {
    var text = messageText(row);
    if (!text) return;
    function copied() {
      var previous = button.title;
      button.title = "Copied";
      button.setAttribute("aria-label", "Response copied");
      window.setTimeout(function () {
        button.title = previous || "Copy";
        button.setAttribute("aria-label", "Copy response");
      }, 1200);
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(copied).catch(function () {});
      return;
    }
    var temp = document.createElement("textarea");
    temp.value = text;
    temp.setAttribute("readonly", "readonly");
    temp.style.position = "fixed";
    temp.style.opacity = "0";
    document.body.appendChild(temp);
    temp.select();
    try {
      if (document.execCommand("copy")) copied();
    } catch (_error) {
      // Clipboard access can be denied by the browser; the response stays intact.
    }
    temp.remove();
  }

  function refreshRegenerateActions(root) {
    if (!root) return;
    var rows = root.querySelectorAll(".chat-row");
    var latest = rows.length ? rows[rows.length - 1] : null;
    var latestReply = latest && latest.querySelector(".reply");
    var canRegenerate = !!(
      latest && latest.classList.contains("orbit") && !latest.classList.contains("was-stopped") &&
      latest.querySelector(".message-actions") && latestReply && latestReply.textContent.trim() &&
      !latest.querySelector(".notice.error")
    );
    Array.prototype.forEach.call(root.querySelectorAll('[data-message-action="regenerate"]'), function (button) {
      button.disabled = !canRegenerate || button.closest(".chat-row") !== latest;
    });
  }

  function wireMessageActions(root, onRegenerate) {
    if (!root) return;
    root.addEventListener("click", function (event) {
      var button = event.target.closest ? event.target.closest(".message-action") : null;
      if (!button || !root.contains(button) || button.disabled) return;
      var row = button.closest(".chat-row");
      var action = button.getAttribute("data-message-action");
      if (!row) return;
      if (action === "copy") {
        copyMessage(button, row);
      } else if (action === "regenerate") {
        if (typeof onRegenerate === "function") onRegenerate(row);
      } else if (action === "feedback-up" || action === "feedback-down") {
        var actions = button.closest(".message-actions");
        Array.prototype.forEach.call(actions.querySelectorAll('[data-message-action^="feedback-"]'), function (choice) {
          choice.setAttribute("aria-pressed", choice === button && choice.getAttribute("aria-pressed") !== "true" ? "true" : "false");
        });
      }
    });
    refreshRegenerateActions(root);
  }

  function setSendButton(button, generating) {
    if (!button) return;
    button.classList.toggle("is-stop", !!generating);
    button.textContent = generating ? "■" : "➤";
    button.setAttribute("aria-label", generating ? "Stop generating" : "Send message");
    button.title = generating ? "Stop generating" : "Send message";
    button.disabled = false;
  }

  // Orbit bubble with its own mini thinking block + answer body.
  function makeOrbitBubble(log, existingRow) {
    var message;
    if (existingRow) {
      var existingBubble = existingRow.querySelector(".chat-bubble.orbit");
      if (!existingBubble) return makeOrbitBubble(log);
      existingRow.classList.remove("is-generating", "is-streaming", "has-error", "was-stopped");
      existingBubble.innerHTML = "";
      message = { row: existingRow, bubble: existingBubble };
    } else {
      message = makeRow(log, "orbit");
    }
    var bubble = message.bubble;
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
      row: message.row,
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

  // --- live turn status ---------------------------------------------------
  // A status line is only ever shown when it is true. Waiting on the model says
  // so; a long wait says so too; and once text is arriving the label describes
  // what is arriving. Nothing here claims work the stream has not shown — a
  // wrong status is worse than no status, and this project has already paid for
  // one ("Orbit is working" that stayed up while nothing was).
  var SLOW_TURN_MS = 12000;

  function TurnStatus(bar, textNode) {
    this.bar = bar;
    this.textNode = textNode;
    this.timer = null;
    this.waiting = false;
  }

  TurnStatus.prototype.begin = function () {
    var self = this;
    this.waiting = true;
    this._say("Thinking…");
    this._clearTimer();
    this.timer = window.setTimeout(function () {
      if (self.waiting) self._say("Still thinking…");
    }, SLOW_TURN_MS);
  };

  // Called when content starts to arrive: the label now describes that content.
  TurnStatus.prototype.arriving = function (text) {
    this.waiting = false;
    this._clearTimer();
    this._say(text);
  };

  TurnStatus.prototype.clear = function () {
    this.waiting = false;
    this._clearTimer();
    if (this.bar) this.bar.hidden = true;
    if (this.textNode) this.textNode.textContent = "";
  };

  TurnStatus.prototype._say = function (text) {
    if (!this.bar) return;
    this.bar.hidden = false;
    if (this.textNode) this.textNode.textContent = text;
  };

  TurnStatus.prototype._clearTimer = function () {
    if (this.timer) {
      window.clearTimeout(this.timer);
      this.timer = null;
    }
  };

  function turnStatusFor(barId, textId) {
    var bar = document.getElementById(barId);
    if (!bar) return new TurnStatus(null, null);
    return new TurnStatus(bar, document.getElementById(textId));
  }

  // --- suggested next steps ------------------------------------------------
  // Follow-up chips are only rendered when the server says the last reply gives
  // them something real to act on (app/followups.py). An empty list hides the
  // row: a chip that suggests something Orbit was never asked about is worse
  // than no chip at all.
  function renderFollowups(box, items) {
    if (!box) return;
    box.textContent = "";
    if (!items || !items.length) {
      box.hidden = true;
      return;
    }
    items.forEach(function (item) {
      var chip = el("button", "followup-chip");
      chip.type = "button";
      chip.textContent = item.label;
      chip.setAttribute("data-followup", item.prompt);
      box.appendChild(chip);
    });
    box.hidden = false;
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

  function wireSettingsControls() {
    var dialog = document.getElementById("settingsDialog");
    if (!dialog || dialog.dataset.orbitWired === "true") return;
    dialog.dataset.orbitWired = "true";
    Array.prototype.forEach.call(document.querySelectorAll("[data-open-settings]"), function (button) {
      button.addEventListener("click", function () {
        if (typeof dialog.showModal === "function") dialog.showModal();
        else dialog.setAttribute("open", "open");
      });
    });
    dialog.addEventListener("click", function (event) {
      if (event.target === dialog && typeof dialog.close === "function") dialog.close();
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
    setTopbarConversationTitle(title);
  }

  function setHistoryTitle(id, title) {
    var item = findHistoryItem(id);
    if (item) item.textContent = title;
    else addHistoryItem(id, title);
    setTopbarConversationTitle(title);
  }

  function setTopbarConversationTitle(title) {
    var container = document.getElementById("currentConversationTitle");
    var name = container && container.querySelector(".topbar-name");
    if (name) name.textContent = title;
    if (container) container.title = title;
  }

  function wireConversationSearch() {
    var input = document.getElementById("conversationSearch");
    if (!input) return;
    input.addEventListener("input", function () {
      var query = input.value.trim().toLocaleLowerCase();
      Array.prototype.forEach.call(document.querySelectorAll(".history-group"), function (group) {
        var visible = 0;
        Array.prototype.forEach.call(group.querySelectorAll(".history-item"), function (item) {
          var match = !query || item.textContent.toLocaleLowerCase().indexOf(query) !== -1;
          item.hidden = !match;
          if (match) visible += 1;
        });
        group.hidden = visible === 0;
      });
    });
  }

  function wireAttachments(buttonId, inputId, noteId) {
    var button = document.getElementById(buttonId);
    var input = document.getElementById(inputId);
    var note = document.getElementById(noteId);
    if (!button || !input || !note) return;
    button.addEventListener("click", function () { input.click(); });
    input.addEventListener("change", function () {
      var files = Array.prototype.map.call(input.files || [], function (file) { return file.name; });
      note.hidden = files.length === 0;
      note.textContent = files.length
        ? "Selected locally: " + files.join(", ") + ". File uploads aren't enabled yet."
        : "";
    });
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

  // --- mode and response-depth selectors ---------------------------------
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

  function wireThinkingToggles() {
    var buttons = document.querySelectorAll(".thinking-toggle button[data-thinking-level]");
    var active = document.querySelector(".thinking-toggle button.active[data-thinking-level]");
    var currentLevel = active ? active.getAttribute("data-thinking-level") : "quick";

    function applyLevel(level) {
      currentLevel = level;
      Array.prototype.forEach.call(buttons, function (button) {
        var selected = button.getAttribute("data-thinking-level") === level;
        button.classList.toggle("active", selected);
        button.setAttribute("aria-pressed", selected ? "true" : "false");
      });
    }

    Array.prototype.forEach.call(buttons, function (button) {
      button.addEventListener("click", function () {
        var level = button.getAttribute("data-thinking-level");
        if (level === currentLevel || button.disabled) return;
        Array.prototype.forEach.call(buttons, function (item) { item.disabled = true; });
        fetch("/api/thinking-level", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ thinking_level: level }),
        })
          .then(function (response) {
            return response.json().then(function (payload) {
              if (!response.ok) throw new Error(payload.error || "Could not change response depth.");
              return payload;
            });
          })
          .then(function (payload) { applyLevel(payload.thinking_level); })
          .catch(function (error) {
            applyLevel(currentLevel);
            console.error("Could not update response depth:", error);
          })
          .finally(function () {
            Array.prototype.forEach.call(buttons, function (item) { item.disabled = false; });
          });
      });
    });
  }

  function setVoiceStatus(node, message, isError) {
    if (!node) return;
    node.textContent = message || "";
    node.hidden = !message;
    node.classList.toggle("error", !!isError);
  }

  function recordingExtension(mimeType) {
    var mime = (mimeType || "").toLowerCase();
    if (mime.indexOf("ogg") !== -1) return "ogg";
    if (mime.indexOf("mp4") !== -1 || mime.indexOf("m4a") !== -1) return "mp4";
    if (mime.indexOf("wav") !== -1) return "wav";
    if (mime.indexOf("mpeg") !== -1 || mime.indexOf("mp3") !== -1) return "mp3";
    return "webm";
  }

  // Press and hold to record; releasing stops the recorder, transcribes the
  // clip, and hands plain text back to the same send handler used by typing.
  function wirePushToTalk(button, statusNode, onTranscript, isBusy) {
    if (!button) return;
    var supported = !!(
      navigator.mediaDevices && navigator.mediaDevices.getUserMedia &&
      typeof window.MediaRecorder !== "undefined"
    );
    var recorder = null;
    var stream = null;
    var chunks = [];
    var pressed = false;
    var keyboardHeld = false;
    var processing = false;
    var maxDurationTimer = null;

    function setActive(active) {
      button.classList.toggle("recording", active);
      button.setAttribute("aria-pressed", active ? "true" : "false");
      button.setAttribute("aria-label", active ? "Recording; release to transcribe" : "Hold to talk");
      button.title = active ? "Release to transcribe" : "Hold to talk";
    }

    function stopTracks(mediaStream) {
      if (mediaStream) {
        mediaStream.getTracks().forEach(function (track) { track.stop(); });
      }
    }

    function updateDisabled() {
      button.disabled = !supported || processing || !!(isBusy && isBusy());
    }

    if (!supported) {
      button.disabled = true;
      setVoiceStatus(statusNode, "Voice recording needs a browser with microphone support over HTTPS or localhost.", true);
      return;
    }

    async function begin(event, fromKeyboard) {
      if (event && event.preventDefault) event.preventDefault();
      if (pressed || processing || (isBusy && isBusy())) return;
      pressed = true;
      keyboardHeld = !!fromKeyboard;
      setVoiceStatus(statusNode, "Requesting microphone access…");
      try {
        var mediaStream = await navigator.mediaDevices.getUserMedia({ audio: true });
        if (!pressed) {
          stopTracks(mediaStream);
          return;
        }
        stream = mediaStream;
        chunks = [];
        var mimeCandidates = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus"];
        var mimeType = "";
        for (var i = 0; i < mimeCandidates.length; i++) {
          if (window.MediaRecorder.isTypeSupported && window.MediaRecorder.isTypeSupported(mimeCandidates[i])) {
            mimeType = mimeCandidates[i];
            break;
          }
        }
        recorder = mimeType ? new window.MediaRecorder(stream, { mimeType: mimeType }) : new window.MediaRecorder(stream);
        var activeRecorder = recorder;
        var activeStream = stream;
        activeRecorder.addEventListener("dataavailable", function (e) {
          if (e.data && e.data.size) chunks.push(e.data);
        });
        activeRecorder.addEventListener("stop", function () {
          var blob = new Blob(chunks, { type: activeRecorder.mimeType || mimeType || "audio/webm" });
          chunks = [];
          stopTracks(activeStream);
          stream = null;
          recorder = null;
          if (!blob.size) {
            setVoiceStatus(statusNode, "The recording was empty. Hold the mic button and try again.", true);
            updateDisabled();
            return;
          }
          processing = true;
          updateDisabled();
          setVoiceStatus(statusNode, "Transcribing…");
          var form = new FormData();
          form.append("audio", blob, "recording." + recordingExtension(blob.type));
          fetch("/api/voice/transcribe", { method: "POST", body: form })
            .then(function (response) {
              return response.json().then(function (payload) {
                if (!response.ok) throw new Error(payload.error || "Transcription failed.");
                return payload;
              });
            })
            .then(function (payload) {
              var transcript = String(payload.transcript || "").trim();
              if (!transcript) throw new Error("No speech was detected. Try again.");
              setVoiceStatus(statusNode, "Sending transcript…");
              if (onTranscript) onTranscript(transcript);
            })
            .catch(function (error) {
              setVoiceStatus(statusNode, error.message || "Could not transcribe that recording.", true);
            })
            .finally(function () {
              processing = false;
              updateDisabled();
            });
        });
        activeRecorder.addEventListener("error", function () {
          setVoiceStatus(statusNode, "The microphone recording failed. Please try again.", true);
        });
        activeRecorder.start();
        setActive(true);
        setVoiceStatus(statusNode, "Listening… release to send.");
        maxDurationTimer = window.setTimeout(function () {
          if (pressed) {
            setVoiceStatus(statusNode, "Recording limit reached; transcribing the clip.");
            release();
          }
        }, 120000);
      } catch (error) {
        pressed = false;
        keyboardHeld = false;
        stopTracks(stream);
        stream = null;
        recorder = null;
        setActive(false);
        setVoiceStatus(statusNode, error.message || "Microphone permission was denied.", true);
        updateDisabled();
      }
    }

    function release() {
      if (!pressed) return;
      pressed = false;
      keyboardHeld = false;
      if (maxDurationTimer) window.clearTimeout(maxDurationTimer);
      maxDurationTimer = null;
      setActive(false);
      if (recorder && recorder.state === "recording") {
        setVoiceStatus(statusNode, "Transcribing…");
        recorder.stop();
      } else {
        stopTracks(stream);
        stream = null;
        setVoiceStatus(statusNode, "");
      }
    }

    button.addEventListener("pointerdown", function (event) {
      if (event.button !== 0) return;
      begin(event, false);
    });
    window.addEventListener("pointerup", release);
    window.addEventListener("pointercancel", release);
    button.addEventListener("keydown", function (event) {
      if (event.key === " " || event.key === "Enter") begin(event, true);
    });
    window.addEventListener("keyup", function (event) {
      if (keyboardHeld && (event.key === " " || event.key === "Enter")) release();
    });
    window.addEventListener("blur", release);
    document.addEventListener("visibilitychange", function () {
      if (document.hidden) release();
    });
    button.addEventListener("contextmenu", function (event) { event.preventDefault(); });
    updateDisabled();
  }

  function showAudioError(body, error) {
    var detail = error && error.message ? error.message : "could not synthesize speech";
    showLine(body, "Spoken reply unavailable: " + detail, "warn");
  }

  function playReplyAudio(text, body) {
    fetch("/api/voice/speech", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: text }),
    })
      .then(function (response) {
        if (!response.ok) {
          return response.json().then(function (payload) {
            throw new Error(payload.error || "Speech synthesis failed.");
          });
        }
        return response;
      })
      .then(function (response) {
        var audio = el("audio", "reply-audio");
        audio.controls = true;
        audio.autoplay = true;
        audio.preload = "auto";
        audio.setAttribute("aria-label", "Play Orbit's spoken reply");
        audio.addEventListener("error", function () {
          showAudioError(body, new Error("the audio stream could not be played"));
        });
        body.appendChild(audio);

        var canStream = !!(
          window.MediaSource && window.MediaSource.isTypeSupported &&
          window.MediaSource.isTypeSupported("audio/mpeg") && response.body && response.body.getReader
        );
        if (!canStream) {
          return response.blob().then(function (blob) {
            audio.src = URL.createObjectURL(blob);
            var playback = audio.play();
            if (playback && playback.catch) playback.catch(function () {});
          });
        }

        var mediaSource = new window.MediaSource();
        audio.src = URL.createObjectURL(mediaSource);
        mediaSource.addEventListener("sourceopen", function () {
          var sourceBuffer;
          try {
            sourceBuffer = mediaSource.addSourceBuffer("audio/mpeg");
          } catch (error) {
            showAudioError(body, error);
            return;
          }
          var reader = response.body.getReader();
          var queued = [];
          var ended = false;
          var reading = false;
          function appendNext() {
            if (sourceBuffer.updating) return;
            if (queued.length) {
              try {
                sourceBuffer.appendBuffer(queued.shift());
              } catch (error) {
                showAudioError(body, error);
              }
              return;
            }
            if (ended) {
              if (mediaSource.readyState === "open") mediaSource.endOfStream();
              return;
            }
            if (reading) return;
            reading = true;
            reader.read().then(function (result) {
              reading = false;
              if (result.done) {
                ended = true;
                appendNext();
                return;
              }
              var chunk = result.value;
              queued.push(chunk.buffer.slice(chunk.byteOffset, chunk.byteOffset + chunk.byteLength));
              appendNext();
            }).catch(function (error) {
              reading = false;
              showAudioError(body, error);
              if (mediaSource.readyState === "open") mediaSource.endOfStream("decode");
            });
          }
          sourceBuffer.addEventListener("updateend", function () {
            var playback = audio.play();
            if (playback && playback.catch) playback.catch(function () {});
            appendNext();
          });
          appendNext();
        }, { once: true });
      })
      .catch(function (error) { showAudioError(body, error); });
  }

  // --- HOME --------------------------------------------------------------
  function bootstrapHome() {
    wireModeToggles();
    wireThinkingToggles();
    wireNewConversation();
    wireSettingsControls();
    wireReminders();
    wireConversationSearch();
    wireAttachments("homeAttach", "homeAttachmentInput", "homeAttachmentNote");
    // Cards still waiting on a decision (rendered by the server on reload).
    Array.prototype.forEach.call(document.querySelectorAll(".action-card[data-state='pending']"), function (container) {
      var summary = container.querySelector(".action-summary");
      renderActionCard(container, {
        id: container.getAttribute("data-action-id"),
        summary: summary ? summary.textContent : "",
        state: "pending",
      });
    });
    var welcome = document.getElementById("welcome");
    var input = document.getElementById("homeInput");
    var send = document.getElementById("homeSend");
    var voiceButton = document.getElementById("homeVoice");
    var voiceStatus = document.getElementById("homeVoiceStatus");
    var thread = document.getElementById("homeThread");
    var followupsBox = document.getElementById("followupChips");
    var turnStatus = turnStatusFor("turnStatusBar", "turnStatusText");
    if (!input || !send || !thread) return;

    // The welcome screen's three shortcuts send a real prompt through the
    // ordinary composer path — they are starting points, not labels.
    Array.prototype.forEach.call(document.querySelectorAll(".welcome-action[data-starter]"), function (button) {
      button.addEventListener("click", function () {
        sendPrompt(button.getAttribute("data-starter"));
      });
    });

    // One ring instance, two speeds. Idle on the welcome screen the mark turns
    // slowly; the moment Orbit starts responding the same mark shrinks, speeds
    // up and sits beside the status line.
    function welcomeResponding() {
      if (welcome) welcome.classList.add("is-responding");
      if (turnStatus.bar) turnStatus.bar.classList.add("no-rings");
    }

    function welcomeDone() {
      if (welcome) welcome.hidden = true;
    }

    // The turn did not land, so the welcome screen goes back to idle with its
    // shortcuts available rather than staying half-collapsed.
    function welcomeIdle() {
      if (welcome) welcome.classList.remove("is-responding");
      if (turnStatus.bar) turnStatus.bar.classList.remove("no-rings");
    }

    // Follow-up chips: one click sends the prompt they stand for. Delegated, so
    // chips rendered by the server on page load behave like streamed ones.
    if (followupsBox) {
      followupsBox.addEventListener("click", function (event) {
        var chip = event.target.closest ? event.target.closest(".followup-chip") : null;
        if (!chip) return;
        sendPrompt(chip.getAttribute("data-followup"));
      });
    }

    // One turn at a time. The send control becomes a cancel action while the
    // active stream is being read; a typed Enter still never discards input.
    var inflight = false;
    var activeRequest = null;
    wireMessageActions(thread, regenerateHome);

    function startStream(row, body, reply, payload, voiceReply) {
      inflight = true;
      activeRequest = window.AbortController ? new window.AbortController() : null;
      var requestController = activeRequest;
      setSendButton(send, true);
      if (voiceButton) voiceButton.disabled = true;
      setVoiceStatus(voiceStatus, "");
      row.classList.add("is-generating");
      body.classList.add("streaming");
      refreshRegenerateActions(thread);
      var fullReply = "";
      var sawText = false;
      turnStatus.begin();
      welcomeResponding();
      thread.scrollTop = thread.scrollHeight;

      postSSE(
        "/api/chat",
        payload,
        function (event, data) {
          if (event === "answer") {
            if (!sawText) {
              sawText = true;
              // The first chunk is on screen, so the label stops saying we are
              // waiting and starts saying what is arriving.
              turnStatus.arriving("Writing your answer…");
            }
            row.classList.remove("is-generating");
            row.classList.add("is-streaming");
            reply.textContent += data;
            fullReply += data;
            thread.scrollTop = thread.scrollHeight;
          } else if (event === "conversation") {
            var created = JSON.parse(data);
            addHistoryItem(created.id, created.title);
          } else if (event === "title") {
            var named = JSON.parse(data);
            setHistoryTitle(named.id, named.title);
          } else if (event === "action") {
            appendActionCard(thread, JSON.parse(data));
            refreshRegenerateActions(thread);
          } else if (event === "followups") {
            renderFollowups(followupsBox, JSON.parse(data));
          } else if (event === "notice") {
            showLine(body, data, "warn");
          } else if (event === "done") {
            body.classList.remove("streaming");
            turnStatus.clear();
          } else if (event === "error") {
            row.classList.add("has-error");
            body.classList.remove("streaming");
            turnStatus.clear();
            showLine(body, data, "error");
          }
        },
        function (err) {
          var stopped = !!(requestController && requestController.signal.aborted);
          inflight = false;
          activeRequest = null;
          setSendButton(send, false);
          if (voiceButton) voiceButton.disabled = false;
          turnStatus.clear();
          row.classList.remove("is-generating", "is-streaming");
          row.classList.toggle("was-stopped", stopped);
          body.classList.remove("streaming");
          if (err) {
            row.classList.add("has-error");
            showLine(body, err, "error");
            welcomeIdle();
          } else {
            // Answered or stopped: the conversation has started, so the welcome
            // screen has done its job and steps out.
            welcomeDone();
            if (voiceReply && fullReply.trim()) playReplyAudio(fullReply, body);
          }
          refreshRegenerateActions(thread);
        },
        activeRequest ? activeRequest.signal : undefined
      );
    }

    function regenerateHome(row) {
      if (inflight || !row || row !== thread.querySelector(".chat-row:last-child")) return;
      var body = row.querySelector(".body");
      var reply = row.querySelector(".reply");
      if (!body || !reply || !reply.textContent.trim()) return;
      Array.prototype.forEach.call(body.querySelectorAll(".notice"), function (notice) { notice.remove(); });
      reply.textContent = "";
      row.classList.remove("has-error");
      renderFollowups(followupsBox, []);
      startStream(row, body, reply, { regenerate: true }, false);
    }

    // A starter button or a follow-up chip: put the prompt in the composer and
    // send it through the ordinary path, so it is a real turn, not a decoration.
    function sendPrompt(prompt) {
      if (!prompt) return;
      if (inflight) {
        var pending = thread.querySelector(".chat-row.orbit:last-child .body");
        if (pending) showLine(pending, "Still waiting on the reply above — send this after it finishes.", "warn");
        return;
      }
      input.value = prompt;
      sendMessage();
    }

    function sendMessage(options) {
      var opts = options || {};
      var voiceReply = !!opts.voiceReply;
      var text = input.value.trim();
      if (!text) return;
      if (inflight) {
        var pending = thread.querySelector(".chat-row.orbit:last-child .body");
        if (pending) showLine(pending, "Still waiting on the reply above — send this after it finishes.", "warn");
        return;
      }
      setVoiceStatus(voiceStatus, "");
      input.value = "";
      // Suggestions belong to the reply that produced them, so they go the
      // moment a new turn starts and come back only if the next reply earns them.
      renderFollowups(followupsBox, []);
      dockComposer();
      thread.classList.add("visible");
      makeUserBubble(thread, text);
      var orbitMessage = makeRow(thread, "orbit");
      var parts = makeOrbitBody(orbitMessage.bubble);
      refreshRegenerateActions(thread);
      startStream(orbitMessage.row, parts.body, parts.reply, { message: text }, voiceReply);
    }

    function stopStream() {
      if (activeRequest) activeRequest.abort();
    }
    send.addEventListener("click", function () {
      if (inflight) stopStream();
      else sendMessage();
    });
    input.addEventListener("keydown", function (e) {
      if (e.key === "Enter") sendMessage();
    });
    if (send.tagName === "BUTTON" && send.form) {
      send.form.addEventListener("submit", function (e) {
        e.preventDefault();
        if (inflight) stopStream();
        else sendMessage();
      });
    }
    wirePushToTalk(voiceButton, voiceStatus, function (transcript) {
      input.value = transcript;
      sendMessage({ voiceReply: true });
    }, function () { return inflight; });
  }

  // --- TASK PANEL ---------------------------------------------------------
  // The panel is drawn from the run state the server streams (app/taskplan.py).
  // Every line is either planned from the request text or taken from what the
  // stream actually produced, so nothing here fills a gap with a guess.
  var RUN_LABEL = { running: "IN PROGRESS", idle: "WAITING FOR YOU", failed: "STOPPED" };
  var CHECK_ICON = { done: "✓", active: "◐", pending: "○" };

  function setText(id, text) {
    var node = document.getElementById(id);
    if (node) node.textContent = text;
  }

  function renderSteps(steps) {
    var list = document.getElementById("stepChecklist");
    if (!list) return;
    list.textContent = "";
    (steps || []).forEach(function (step) {
      var item = el("li", "checklist-step " + step.status);
      var icon = el("span", "check-icon", CHECK_ICON[step.status] || CHECK_ICON.pending);
      icon.setAttribute("aria-hidden", "true");
      var body = el("span", "check-body");
      body.appendChild(el("span", "check-title", escapeHtml(step.title)));
      // The sentence from Orbit's narration that moved this step, when there is
      // one. A step with no line behind it stays "Not started".
      var detail = step.evidence ? "Orbit: “" + step.evidence + "”" : "Not started";
      body.appendChild(el("span", "check-detail", escapeHtml(detail)));
      item.appendChild(icon);
      item.appendChild(body);
      list.appendChild(item);
    });
  }

  function renderFiles(files) {
    var box = document.getElementById("fileChips");
    var empty = document.getElementById("filesEmpty");
    var names = files || [];
    if (box) {
      box.textContent = "";
      names.forEach(function (item) {
        var chip = el("span", "file-chip", escapeHtml(item.name));
        chip.appendChild(el("em", null, item.source === "request" ? "your request" : "Orbit"));
        box.appendChild(chip);
      });
    }
    if (empty) empty.hidden = names.length > 0;
    setText("statusBarFiles", names.length + " file(s) named");
  }

  function renderOperation(state) {
    var node = document.getElementById("currentOperation");
    if (node) {
      node.className =
        "operation" +
        (state.status === "running" ? " is-running" : state.status === "failed" ? " is-failed" : " is-idle");
      node.textContent = "";
      node.appendChild(el("span", "dot"));
      node.appendChild(el("span", "op-text", escapeHtml(state.operation || "Not running")));
    }
    // The topbar indicator only exists while a run is genuinely in flight.
    var indicator = document.getElementById("workingIndicator");
    if (indicator) {
      indicator.hidden = state.status !== "running";
      setText("workingIndicatorText", state.operation || "");
    }
  }

  function renderActivity(rows) {
    var section = document.getElementById("taskActivity");
    if (!section) return;
    Array.prototype.forEach.call(section.querySelectorAll(".activity-row"), function (row) { row.remove(); });
    var list = rows || [];
    section.hidden = list.length === 0;
    if (!list.length) return;
    var heading = section.querySelector("h4");
    var fragment = document.createDocumentFragment();
    list.forEach(function (row) {
      var line = el("div", "activity-row");
      line.appendChild(el("span", null, escapeHtml(row.text)));
      line.appendChild(el("span", "time", escapeHtml(row.time)));
      fragment.appendChild(line);
    });
    section.insertBefore(fragment, heading ? heading.nextSibling : section.firstChild);
  }

  function renderTaskPanel(state) {
    var panel = document.getElementById("taskPanel");
    if (!state || !panel) return;
    panel.hidden = false;
    var startBox = document.getElementById("taskStart");
    if (startBox) startBox.hidden = true;
    setText("taskRunState", RUN_LABEL[state.status] || "NOT STARTED");
    renderSteps(state.steps);
    renderFiles(state.files);
    renderOperation(state);
    var approval = document.getElementById("approvalStateLine");
    if (approval) {
      approval.textContent = state.applied
        ? "✓ Changes applied."
        : state.approved
          ? "✓ You approved — safe to apply."
          : "Nothing is waiting on your approval.";
    }
    renderActivity(state.activity);
  }

  // --- TASK --------------------------------------------------------------
  function bootstrapTask(taskId, options) {
    var opts = options || {};
    // Whether the page should ask for the task's opening update on load. The
    // server decides (see app.server.task) so a refresh is not a model call.
    var wantInitialTurn = opts.initialTurn !== false;
    wireModeToggles();
    wireThinkingToggles();
    wireNewConversation();
    wireSettingsControls();
    wireReminders();
    wireConversationSearch();
    wireAttachments("taskAttach", "taskAttachmentInput", "taskAttachmentNote");
    var log = document.getElementById("taskChatLog");
    var thinkBlock = document.getElementById("thinkingBlock");
    var thinkText = document.getElementById("thinkingText");
    var thinkLabel = document.getElementById("thinkingLabel");
    var input = document.getElementById("taskInput");
    var send = document.getElementById("taskSend");
    var voiceButton = document.getElementById("taskVoice");
    var voiceStatus = document.getElementById("taskVoiceStatus");
    var inflight = false;
    var activeRequest = null;
    var turnStatus = turnStatusFor("taskTurnStatus", "taskTurnStatusText");
    wireMessageActions(log, regenerateTask);

    // A task that has not run has no panel: the start box offers the request and
    // a way to begin (or to describe a new one).
    var startBox = document.getElementById("taskStart");
    var startExisting = document.getElementById("taskStartExisting");
    var composeBox = document.getElementById("taskStartCompose");
    var newTaskInput = document.getElementById("newTaskInput");
    var newTaskError = document.getElementById("newTaskError");
    var hasRun = !!(document.getElementById("taskPanel") && !document.getElementById("taskPanel").hidden);

    function showStartBox(compose) {
      if (!startBox || !startExisting || !composeBox) return;
      startBox.hidden = false;
      startExisting.hidden = !!compose;
      composeBox.hidden = !compose;
      var panel = document.getElementById("taskPanel");
      if (panel) panel.hidden = true;
      if (compose && newTaskInput) newTaskInput.focus();
    }

    function restoreAfterCompose() {
      if (newTaskError) newTaskError.textContent = "";
      var panel = document.getElementById("taskPanel");
      if (hasRun && panel) {
        panel.hidden = false;
        if (startBox) startBox.hidden = true;
        return;
      }
      showStartBox(false);
    }

    var newTaskBtn = document.getElementById("newTaskBtn");
    if (newTaskBtn) {
      newTaskBtn.addEventListener("click", function () {
        if (newTaskError) newTaskError.textContent = "";
        showStartBox(true);
      });
    }

    var cancelTaskBtn = document.getElementById("cancelTaskBtn");
    if (cancelTaskBtn) cancelTaskBtn.addEventListener("click", restoreAfterCompose);

    var createTaskBtn = document.getElementById("createTaskBtn");
    if (createTaskBtn && newTaskInput) {
      createTaskBtn.addEventListener("click", function () {
        var request = newTaskInput.value.trim();
        if (!request) {
          if (newTaskError) newTaskError.textContent = "Describe what Orbit should work on first.";
          newTaskInput.focus();
          return;
        }
        createTaskBtn.disabled = true;
        if (newTaskError) newTaskError.textContent = "";
        fetch("/api/tasks", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ request: request }),
        })
          .then(function (res) {
            return res.json().catch(function () { return {}; }).then(function (data) {
              if (!res.ok) throw new Error(data.error || "HTTP " + res.status);
              return data;
            });
          })
          .then(function (data) {
            // The new task has no reply yet, so its opening turn runs on load.
            window.location.href = "/task/" + encodeURIComponent(data.id);
          })
          .catch(function (err) {
            if (newTaskError) newTaskError.textContent = (err && err.message) || "Could not start that task.";
            createTaskBtn.disabled = false;
          });
      });
    }

    var startTaskBtn = document.getElementById("startTaskBtn");
    if (startTaskBtn) {
      startTaskBtn.addEventListener("click", function () {
        streamTurn({ initial: true, useTopThinking: true });
      });
    }

    // Stream one Orbit turn. The page-level block narrates the opening turn;
    // follow-up turns keep their own compact thought block in the chat.
    function streamTurn(opts) {
      opts = opts || {};
      if (inflight) return;
      inflight = true;
      activeRequest = window.AbortController ? new window.AbortController() : null;
      var requestController = activeRequest;
      setSendButton(send, true);
      if (voiceButton) voiceButton.disabled = true;
      setVoiceStatus(voiceStatus, "");
      var useTop = !!opts.useTopThinking;
      var orbit = makeOrbitBubble(log, opts.replaceRow || null);
      var fullReply = "";
      var sawText = false;
      var targetThink = useTop ? thinkText : orbit.thinkText;
      var targetBlock = useTop ? thinkBlock : orbit.think;
      var targetLabel = useTop ? thinkLabel : orbit.thinkLabel;
      if (!useTop && targetBlock) targetBlock.style.display = "none";
      orbit.row.classList.add("is-generating");
      orbit.body.classList.add("streaming");
      // The panel has state to show as soon as the run exists.
      hasRun = true;
      if (startBox) startBox.hidden = true;
      var panelNode = document.getElementById("taskPanel");
      if (panelNode) panelNode.hidden = false;
      turnStatus.begin();
      refreshRegenerateActions(log);

      function revealThinking() {
        if (!useTop && targetBlock) targetBlock.style.display = "";
      }

      postSSE(
        "/api/task/" + encodeURIComponent(taskId) + "/stream",
        { message: opts.message || "", initial: !!opts.initial, regenerate: !!opts.regenerate },
        function (event, data) {
          if (event === "task_state") {
            // Steps, files, the current operation, approvals and the activity
            // log — all of it derived from this run, never from seeded numbers.
            renderTaskPanel(JSON.parse(data));
          } else if (event === "status") {
            showModelLabel(targetLabel, data);
          } else if (event === "thinking") {
            if (!sawText) {
              sawText = true;
              turnStatus.arriving("Working through it…");
            }
            if (!targetThink) return;
            revealThinking();
            targetThink.textContent += data;
            log.scrollTop = log.scrollHeight;
          } else if (event === "answer") {
            if (!sawText) turnStatus.arriving("Writing your answer…");
            sawText = true;
            if (targetBlock && !targetBlock.classList.contains("done")) targetBlock.classList.add("done");
            orbit.row.classList.remove("is-generating");
            orbit.row.classList.add("is-streaming");
            orbit.reply.textContent += data;
            fullReply += data;
            log.scrollTop = log.scrollHeight;
          } else if (event === "done") {
            if (targetBlock) targetBlock.classList.add("done");
            orbit.body.classList.remove("streaming");
            turnStatus.clear();
          } else if (event === "error") {
            if (targetBlock) targetBlock.classList.add("done");
            orbit.row.classList.add("has-error");
            orbit.body.classList.remove("streaming");
            turnStatus.clear();
            showLine(orbit.body, data, "error");
          }
        },
        function (err) {
          var stopped = !!(requestController && requestController.signal.aborted);
          inflight = false;
          activeRequest = null;
          setSendButton(send, false);
          if (voiceButton) voiceButton.disabled = false;
          if (targetBlock) targetBlock.classList.add("done");
          turnStatus.clear();
          orbit.row.classList.remove("is-generating", "is-streaming");
          orbit.row.classList.toggle("was-stopped", stopped);
          orbit.body.classList.remove("streaming");
          if (err) {
            orbit.row.classList.add("has-error");
            showLine(orbit.body, err, "error");
          } else if (opts.voiceReply && fullReply.trim()) {
            playReplyAudio(fullReply, orbit.body);
          }
          refreshRegenerateActions(log);
        },
        requestController ? requestController.signal : undefined
      );
    }

    function regenerateTask(row) {
      if (inflight || !row || row !== log.querySelector(".chat-row:last-child")) return;
      var reply = row.querySelector(".reply");
      if (!reply || !reply.textContent.trim()) return;
      streamTurn({ regenerate: true, replaceRow: row, useTopThinking: false });
    }

    if (input && send) {
      function sendSteer(options) {
        var steerOptions = options || {};
        if (inflight) {
          setVoiceStatus(voiceStatus, "Wait for Orbit's current reply to finish before sending another message.", true);
          return;
        }
        var text = input.value.trim();
        if (!text) return;
        input.value = "";
        makeUserBubble(log, text);
        streamTurn({
          message: text,
          useTopThinking: false,
          voiceReply: !!steerOptions.voiceReply,
        });
      }
      send.addEventListener("click", function () {
        if (inflight) {
          if (activeRequest) activeRequest.abort();
          return;
        }
        sendSteer();
      });
      input.addEventListener("keydown", function (e) {
        if (e.key === "Enter") sendSteer();
      });
      wirePushToTalk(voiceButton, voiceStatus, function (transcript) {
        input.value = transcript;
        sendSteer({ voiceReply: true });
      }, function () { return inflight; });
    }

    if (wantInitialTurn) {
      streamTurn({ initial: true, useTopThinking: true });
    } else if (thinkBlock) {
      // Nothing is streaming into the top block, so stop its "thinking" caret
      // rather than leaving a blinking empty box under the header.
      thinkBlock.classList.add("done");
    }

    // The approval gate lives in the store, not in a button: app.store.apply_changes
    // refuses unless the task was explicitly approved, and /api/task/<id>/apply
    // answers 403. There is deliberately no Approve control here — with nothing
    // proposed there is nothing to approve, and a button that only looks like a
    // gate is worse than the honest line the panel shows instead.
  }

  window.Orbit = {
    bootstrapHome: bootstrapHome,
    bootstrapTask: bootstrapTask,
  };
})();
