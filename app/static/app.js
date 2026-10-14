/* ORBIT UI behavior.
 *
 * Replaces the mockup's timer-based "streaming" fake with real Server-Sent
 * Events from ChatProvider.reply_stream. Chunks arrive as they are generated
 * and are appended to the DOM immediately — messages render in strict
 * chronological order and the blinking caret is an inline ::after on the
 * streamed reply text node itself.
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
    var sawDoneEvent = false;
    function event(name, data) {
      if (
        name === "answer" ||
        name === "thinking" ||
        name === "action" ||
        name === "notice"
      ) {
        sawContent = true;
      }
      if (name === "error") sawErrorEvent = true;
      if (name === "done") sawDoneEvent = true;
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
          if (sawErrorEvent) {
            finish(null);
          } else if (!sawContent) {
            finish(
              "The model returned nothing for this turn. Check the key and model in .env, or run the real provider instead of the demo stub."
            );
          } else if (!sawDoneEvent) {
            finish("Response was cut off before finishing.");
          } else {
            finish(null);
          }
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

  // --- auto-scroll --------------------------------------------------------
  // Scroll to the newest message on send and while a response streams in —
  // but only if the user was already scrolled to the bottom. If they have
  // scrolled up to reread something earlier, do not yank them back down until
  // they return to the bottom themselves (or click "Jump to latest").
  var AUTO_SCROLL_THRESHOLD_PX = 40;

  function isScrolledNearBottom(container) {
    if (!container) return true;
    var scrollHeight = Number(container.scrollHeight) || 0;
    var scrollTop = Number(container.scrollTop) || 0;
    var clientHeight = Number(container.clientHeight) || 0;
    if (scrollHeight <= 0) return true;
    if (clientHeight > 0 && scrollHeight <= clientHeight) return true;
    return scrollHeight - scrollTop - clientHeight <= AUTO_SCROLL_THRESHOLD_PX;
  }

  function createAutoScroller(container) {
    if (!container) {
      return {
        captureBeforeChange: function () { return true; },
        scrollToBottomIfPinned: function () {},
        forceScrollToBottom: function () {},
        isPinned: function () { return true; },
      };
    }
    if (container._orbitAutoScroller) return container._orbitAutoScroller;

    var pinned = true;
    var programmaticScroll = false;
    var lastProgrammaticTop = null;
    var jumpButton = null;

    function ensureJumpButton() {
      if (jumpButton || !container.parentNode) return jumpButton;
      jumpButton = el("button", "jump-to-latest", "↓ Jump to latest");
      jumpButton.type = "button";
      jumpButton.hidden = true;
      jumpButton.setAttribute("aria-label", "Jump to latest message");
      jumpButton.addEventListener("click", function () {
        forceScrollToBottom();
      });
      if (container.nextSibling) {
        container.parentNode.insertBefore(jumpButton, container.nextSibling);
      } else {
        container.parentNode.appendChild(jumpButton);
      }
      return jumpButton;
    }

    function updateJumpButton() {
      var btn = ensureJumpButton();
      if (btn) btn.hidden = pinned;
    }

    function captureBeforeChange() {
      var clientHeight = Number(container.clientHeight) || 0;
      if (clientHeight > 0 || lastProgrammaticTop !== null) {
        pinned = isScrolledNearBottom(container);
      }
      updateJumpButton();
      return pinned;
    }

    container.addEventListener("scroll", function () {
      if (programmaticScroll) return;
      pinned = isScrolledNearBottom(container);
      if (pinned) lastProgrammaticTop = Number(container.scrollTop) || 0;
      updateJumpButton();
    });

    function writeBottom() {
      programmaticScroll = true;
      container.scrollTop = Number(container.scrollHeight) || 0;
      lastProgrammaticTop = Number(container.scrollTop) || 0;
      programmaticScroll = false;
    }

    function scrollToBottomIfPinned() {
      if (
        lastProgrammaticTop !== null &&
        Number(container.scrollTop) < lastProgrammaticTop - AUTO_SCROLL_THRESHOLD_PX &&
        !isScrolledNearBottom(container)
      ) {
        pinned = false;
        updateJumpButton();
      }
      if (!pinned) return false;
      writeBottom();
      updateJumpButton();
      return true;
    }

    function forceScrollToBottom() {
      pinned = true;
      writeBottom();
      updateJumpButton();
    }

    var api = {
      captureBeforeChange: captureBeforeChange,
      scrollToBottomIfPinned: scrollToBottomIfPinned,
      forceScrollToBottom: forceScrollToBottom,
      isPinned: function () { return pinned; },
    };
    container._orbitAutoScroller = api;
    return api;
  }

  function maybeScrollToBottom(container) {
    if (!container) return;
    createAutoScroller(container).scrollToBottomIfPinned();
  }

  // One visible line inside an Orbit bubble, used for errors and notices.
  function showLine(body, text, cls) {
    var line = body.querySelector(".notice");
    if (!line) {
      line = el("div", "notice");
      body.appendChild(line);
    }
    line.className = "notice" + (cls ? " " + cls : "");
    line.textContent = text;
    var thread = body.closest("[id]");
    if (thread) maybeScrollToBottom(thread);
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
    var created = makeRow(log, "user");
    created.bubble.appendChild(el("div", "body", escapeHtml(text)));
    return created.row;
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
    if (!reply) return "";
    var raw = reply.getAttribute("data-raw-text");
    return raw !== null ? raw : reply.textContent;
  }

  function writeTextToClipboard(text, onSuccess) {
    if (!text) return;
    if (typeof navigator !== "undefined" && navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(onSuccess).catch(function () {});
      return;
    }
    if (!document.body) {
      onSuccess();
      return;
    }
    var temp = document.createElement("textarea");
    temp.value = text;
    temp.setAttribute("readonly", "readonly");
    temp.style.position = "fixed";
    temp.style.opacity = "0";
    document.body.appendChild(temp);
    if (temp.select) temp.select();
    try {
      if (!document.execCommand || document.execCommand("copy")) onSuccess();
    } catch (_error) {
      // Clipboard access can be denied by the browser; the response stays intact.
    }
    temp.remove();
  }

  function copyMessage(button, row) {
    var text = messageText(row);
    if (!text) return;
    writeTextToClipboard(text, function () {
      var previous = button.title;
      button.title = "Copied";
      button.setAttribute("aria-label", "Response copied");
      window.setTimeout(function () {
        button.title = previous || "Copy";
        button.setAttribute("aria-label", "Copy response");
      }, 1200);
    });
  }

  function copyCodeBlock(button) {
    var block = button && button.closest ? button.closest(".code-block") : null;
    if (!block) return;
    var code = block.getAttribute("data-code");
    if (code === null) {
      var codeNode = block.querySelector(".code-content");
      code = codeNode ? codeNode.textContent : "";
    }
    if (!code) return;
    writeTextToClipboard(code, function () {
      button.setAttribute("data-copied", "true");
      button.textContent = "Copied";
      button.title = "Copied";
      button.setAttribute("aria-label", "Code copied");
      window.setTimeout(function () {
        if (button.removeAttribute) button.removeAttribute("data-copied");
        button.textContent = "Copy";
        button.title = "Copy code";
        button.setAttribute("aria-label", "Copy code");
      }, 1200);
    });
  }

  // --- code blocks & syntax highlighting ----------------------------------
  var CODE_TOKEN_RE =
    /(#[^\n]*|\/\/[^\n]*|\/\*[\s\S]*?\*\/|<!--[\s\S]*?-->)|("""[\s\S]*?"""|'''[\s\S]*?'''|"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'|`(?:\\.|[^`\\])*`)|\b(def|class|return|import|from|if|elif|else|for|while|try|except|finally|raise|with|as|pass|break|continue|lambda|yield|async|await|in|is|not|and|or|function|const|let|var|export|default|new|typeof|instanceof|switch|case|throw|catch|interface|type|extends|implements|public|private|protected|readonly|struct|enum|fn|pub|mut|impl|match|use|mod|crate|where|go|defer|chan|package|func|select|SELECT|FROM|WHERE|INSERT|INTO|VALUES|UPDATE|SET|DELETE|JOIN|LEFT|RIGHT|INNER|ORDER|BY|GROUP|LIMIT)\b|\b(True|False|None|true|false|null|undefined|NaN|Infinity|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|0x[0-9a-fA-F]+)\b|\b([A-Za-z_]\w*)(?=\s*\()/g;

  function highlightDiff(code) {
    return String(code || "")
      .split("\n")
      .map(function (line) {
        var escaped = escapeHtml(line);
        if (/^(?:\+\+\+|---|\+|-)/.test(line)) {
          if (line.indexOf("+") === 0 && line.indexOf("+++") !== 0) {
            return '<span class="tok-diff-add">' + escaped + "</span>";
          }
          if (line.indexOf("-") === 0 && line.indexOf("---") !== 0) {
            return '<span class="tok-diff-del">' + escaped + "</span>";
          }
          return '<span class="tok-diff-hunk">' + escaped + "</span>";
        }
        if (/^(?:@@|diff |index )/.test(line)) {
          return '<span class="tok-diff-hunk">' + escaped + "</span>";
        }
        return escaped;
      })
      .join("\n");
  }

  function looksLikeDiff(code, lang) {
    var cleanLang = String(lang || "").toLowerCase();
    if (cleanLang === "diff" || cleanLang === "patch") return true;
    return /^(?:diff --git|--- |\+\+\+ |@@ )/m.test(String(code || ""));
  }

  function highlightCode(code, lang) {
    var source = String(code || "");
    if (looksLikeDiff(source, lang)) {
      return highlightDiff(source);
    }
    var out = "";
    var lastIdx = 0;
    CODE_TOKEN_RE.lastIndex = 0;
    var match;
    while ((match = CODE_TOKEN_RE.exec(source)) !== null) {
      if (match.index > lastIdx) {
        out += escapeHtml(source.slice(lastIdx, match.index));
      }
      var token = match[0];
      var cls = "tok-keyword";
      if (match[1] !== undefined) cls = "tok-comment";
      else if (match[2] !== undefined) cls = "tok-string";
      else if (match[3] !== undefined) cls = "tok-keyword";
      else if (match[4] !== undefined) cls = "tok-number";
      else if (match[5] !== undefined) cls = "tok-func";
      out += '<span class="' + cls + '">' + escapeHtml(token) + "</span>";
      lastIdx = CODE_TOKEN_RE.lastIndex;
    }
    if (lastIdx < source.length) {
      out += escapeHtml(source.slice(lastIdx));
    }
    return out;
  }

  function appendInlineProse(host, text) {
    if (!text) return;
    var inlineRe = /`([^`\n]+)`/g;
    var lastIdx = 0;
    var match;
    while ((match = inlineRe.exec(text)) !== null) {
      if (match.index > lastIdx) {
        host.appendChild(el("span", "reply-text", escapeHtml(text.slice(lastIdx, match.index))));
      }
      host.appendChild(el("code", "inline-code", escapeHtml(match[1])));
      lastIdx = inlineRe.lastIndex;
    }
    if (lastIdx < text.length) {
      host.appendChild(el("span", "reply-text", escapeHtml(text.slice(lastIdx))));
    }
  }

  function buildCodeBlockElement(code, lang, isOpen) {
    var wrapper = el("div", "code-block" + (isOpen ? " is-open" : ""));
    wrapper.setAttribute("data-code", code);
    wrapper.setAttribute("data-lang", lang || "");
    var header = el("div", "code-block-header");
    header.appendChild(el("span", "code-lang", escapeHtml(lang || (looksLikeDiff(code, lang) ? "diff" : "code"))));
    var copyBtn = el("button", "code-copy-btn", "Copy");
    copyBtn.type = "button";
    copyBtn.setAttribute("data-code-copy", "true");
    copyBtn.setAttribute("aria-label", "Copy code");
    copyBtn.setAttribute("title", "Copy code");
    header.appendChild(copyBtn);
    wrapper.appendChild(header);
    var pre = el("pre", "code-pre");
    var codeEl = el("code", "code-content", highlightCode(code, lang));
    pre.appendChild(codeEl);
    wrapper.appendChild(pre);
    return wrapper;
  }

  function renderReplyContent(reply, rawText) {
    if (!reply) return;
    var source = String(rawText == null ? "" : rawText);
    reply.setAttribute("data-raw-text", source);
    if (source.indexOf("`") === -1) {
      reply.classList.remove("in-code-block");
      reply.textContent = source;
      return;
    }
    reply.textContent = "";
    var fenceRe = /```([a-zA-Z0-9_+-]*)[ \t]*\r?\n?([\s\S]*?)(```|$)/g;
    var lastIdx = 0;
    var match;
    var endsInOpenCodeBlock = false;
    while ((match = fenceRe.exec(source)) !== null) {
      if (match.index > lastIdx) {
        appendInlineProse(reply, source.slice(lastIdx, match.index));
      }
      var lang = (match[1] || "").trim();
      var code = match[2] || "";
      var isOpen = match[3] !== "```";
      if (!isOpen && code.slice(-1) === "\n") {
        code = code.slice(0, -1);
      }
      reply.appendChild(buildCodeBlockElement(code, lang, isOpen));
      lastIdx = fenceRe.lastIndex;
      endsInOpenCodeBlock = isOpen && lastIdx >= source.length;
      if (isOpen) break;
    }
    if (lastIdx < source.length) {
      appendInlineProse(reply, source.slice(lastIdx));
      endsInOpenCodeBlock = false;
    }
    reply.classList.toggle("in-code-block", endsInOpenCodeBlock);
  }

  function formatExistingReplies(root) {
    if (!root) return;
    Array.prototype.forEach.call(root.querySelectorAll(".chat-row.orbit .reply"), function (reply) {
      var raw = reply.getAttribute("data-raw-text");
      var text = raw !== null ? raw : reply.textContent;
      if (text && text.indexOf("`") !== -1) {
        renderReplyContent(reply, text);
      } else if (raw === null) {
        reply.setAttribute("data-raw-text", text || "");
      }
    });
  }

  // --- inline retry / regenerate on failure -------------------------------
  function prevUserRow(row) {
    if (!row) return null;
    if (row._userRow) return row._userRow;
    if (!row.parentNode || !row.parentNode.children) return null;
    var siblings = row.parentNode.children;
    var idx = Array.prototype.indexOf.call(siblings, row);
    for (var i = idx - 1; i >= 0; i--) {
      if (siblings[i].classList && siblings[i].classList.contains("chat-row")) {
        return siblings[i].classList.contains("user") ? siblings[i] : null;
      }
    }
    return null;
  }

  function prevUserRowText(row) {
    var userRow = prevUserRow(row);
    if (!userRow) return "";
    var body = userRow.querySelector(".body");
    return body ? body.textContent.trim() : "";
  }

  function makeRetryButton(label, ariaLabel) {
    var btn = el("button", "inline-retry-btn", label || "↻ Retry");
    btn.type = "button";
    btn.setAttribute("data-message-action", "retry");
    btn.setAttribute("aria-label", ariaLabel || "Retry in place");
    btn.setAttribute("title", ariaLabel || "Retry in place");
    return btn;
  }

  function clearRetryUI(row) {
    if (!row) return;
    if (row.removeAttribute) row.removeAttribute("data-can-retry");
    else row.setAttribute("data-can-retry", "false");
    row.classList.remove("has-error", "was-stopped");
    Array.prototype.forEach.call(row.querySelectorAll(".inline-retry-btn"), function (btn) {
      btn.remove();
    });
    var userRow = prevUserRow(row);
    if (userRow) {
      userRow.classList.remove("has-error");
      Array.prototype.forEach.call(userRow.querySelectorAll(".inline-retry-btn"), function (btn) {
        btn.remove();
      });
    }
  }

  function attachRetryAction(row, body, noAnswerYet) {
    if (!row || !body) return;
    row.setAttribute("data-can-retry", "true");
    var notice = body.querySelector(".notice");
    var host = notice || body;
    if (!host.querySelector(".inline-retry-btn")) {
      host.appendChild(
        makeRetryButton(
          noAnswerYet ? "↻ Retry" : "↻ Regenerate",
          noAnswerYet ? "Retry message in place" : "Regenerate incomplete response in place"
        )
      );
    }
    if (noAnswerYet) {
      var userRow = prevUserRow(row);
      if (userRow) {
        userRow._orbitRow = row;
        userRow.classList.add("has-error");
        if (!userRow.querySelector(".inline-retry-btn")) {
          userRow.appendChild(makeRetryButton("↻ Retry", "Retry sending message"));
        }
      }
    }
  }

  // Regenerate stays available on every reply, so an older answer can be redone
  // too. It is disabled only while a reply is streaming (two turns at once would
  // interleave) or when the row has nothing to regenerate yet.
  function refreshRegenerateActions(root) {
    if (!root) return;
    var anyBusy = !!root.querySelector(".chat-row.is-generating, .chat-row.is-streaming");
    Array.prototype.forEach.call(root.querySelectorAll('[data-message-action="regenerate"]'), function (button) {
      var row = button.closest(".chat-row");
      var text = messageText(row);
      var busy = anyBusy || (!!row && (row.classList.contains("is-generating") || row.classList.contains("is-streaming")));
      var canRetry =
        !!row &&
        (row.getAttribute("data-can-retry") === "true" ||
          row.classList.contains("has-error") ||
          row.classList.contains("was-stopped"));
      button.disabled = !row || busy || (!canRetry && !text.trim());
    });
    Array.prototype.forEach.call(root.querySelectorAll('[data-message-action="retry"]'), function (button) {
      button.disabled = anyBusy;
    });
  }

  function messageId(row) {
    return row ? (row.getAttribute("data-message-id") || "") : "";
  }

  function setMessageId(row, id) {
    if (row && id) row.setAttribute("data-message-id", id);
  }

  // One binary rating per click, recorded server-side against the message id.
  // Clicking the rating that is already showing does not re-send it; clicking
  // the other one switches, and the log then holds one line for each.
  function sendRating(button, row, rating) {
    var id = messageId(row);
    if (!id) return;
    var payload = JSON.stringify({ rating: rating });
    var request = window.fetch
      ? window.fetch("/api/messages/" + encodeURIComponent(id) + "/rating", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: payload,
        })
      : null;
    if (!request || !request.catch) return;
    request.catch(function () {
      // A rating that could not be recorded is not worth interrupting the reply
      // for; the button state is the person's own view either way.
    });
  }

  function wireMessageActions(root, onRegenerate) {
    if (!root) return;
    root.addEventListener("click", function (event) {
      var target = event.target;
      if (!target || !target.closest) return;
      var codeBtn = target.closest("[data-code-copy]");
      if (codeBtn && (!root.contains || root.contains(codeBtn))) {
        copyCodeBlock(codeBtn);
        return;
      }
      var button = target.closest("[data-message-action]");
      if (!button || (root.contains && !root.contains(button)) || button.disabled) return;
      var row = button.closest(".chat-row");
      var action = button.getAttribute("data-message-action");
      if (!row) return;
      if (action === "copy") {
        copyMessage(button, row);
      } else if (action === "regenerate" || action === "retry") {
        var targetRow = row.classList.contains("user") && row._orbitRow ? row._orbitRow : row;
        if (typeof onRegenerate === "function") onRegenerate(targetRow);
      } else if (action === "feedback-up" || action === "feedback-down") {
        var actions = button.closest(".message-actions");
        var pressed = button.getAttribute("aria-pressed") === "true";
        if (pressed) {
          // Un-pressing is a local change of mind; it is not a rating, so it is
          // not logged.
          button.setAttribute("aria-pressed", "false");
          return;
        }
        Array.prototype.forEach.call(actions.querySelectorAll('[data-message-action^="feedback-"]'), function (choice) {
          choice.setAttribute("aria-pressed", choice === button ? "true" : "false");
        });
        sendRating(button, row, action === "feedback-up" ? "up" : "down");
      }
    });
    refreshRegenerateActions(root);
  }

  // --- SIDEBAR COLLAPSE ----------------------------------------------------
  // Icons-only rail. The choice is remembered in localStorage and applied to
  // every screen; each control keeps its tooltip and accessible name.
  var SIDEBAR_STORAGE_KEY = "orbit.sidebar.collapsed";

  function sidebarCollapsed() {
    try {
      return window.localStorage.getItem(SIDEBAR_STORAGE_KEY) === "1";
    } catch (_error) {
      return false;  // private mode or a blocked store: expanded is the default
    }
  }

  function rememberSidebarCollapsed(collapsed) {
    try {
      window.localStorage.setItem(SIDEBAR_STORAGE_KEY, collapsed ? "1" : "0");
    } catch (_error) {
      // Not being able to remember the choice must not break the toggle.
    }
  }

  function applySidebarCollapsed(sidebar, collapsed) {
    sidebar.classList.toggle("sidebar--collapsed", collapsed);
    // The pre-paint root class (layout.html) has done its job once the sidebar
    // itself carries the state.
    if (document.documentElement) {
      document.documentElement.classList.remove("sidebar-collapsed-pending");
    }
    var toggle = sidebar.querySelector("#sidebarToggle");
    if (!toggle) return;
    toggle.setAttribute("aria-pressed", collapsed ? "true" : "false");
    toggle.setAttribute("aria-label", collapsed ? "Expand sidebar" : "Collapse sidebar");
    toggle.title = collapsed ? "Expand sidebar" : "Collapse sidebar";
    toggle.firstElementChild.textContent = collapsed ? "»" : "«";
  }

  function wireSidebarCollapse() {
    var sidebar = document.getElementById("sidebar");
    if (!sidebar) return;
    applySidebarCollapsed(sidebar, sidebarCollapsed());
    var toggle = sidebar.querySelector("#sidebarToggle");
    if (!toggle) return;
    toggle.addEventListener("click", function () {
      var collapsed = !sidebar.classList.contains("sidebar--collapsed");
      applySidebarCollapsed(sidebar, collapsed);
      rememberSidebarCollapsed(collapsed);
    });
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
      existingRow.classList.remove(
        "is-generating",
        "is-streaming",
        "has-thinking",
        "has-error",
        "was-stopped"
      );
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
    maybeScrollToBottom(log);
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

  TurnStatus.prototype.placeBefore = function (node) {
    if (!this.bar || !node || !node.parentNode) return;
    node.parentNode.insertBefore(this.bar, node);
  };

  TurnStatus.prototype.placeAfter = function (node) {
    if (!this.bar || !node || !node.parentNode) return;
    if (node.nextSibling) {
      node.parentNode.insertBefore(this.bar, node.nextSibling);
    } else {
      node.parentNode.appendChild(this.bar);
    }
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

  var STEP_FILE_NAME_RE =
    /(?:^|[^\w./-])[A-Za-z0-9_][\w-]*(?:\/[\w.-]+)*\.(?:py|js|jsx|mjs|cjs|ts|tsx|json|md|markdown|yml|yaml|toml|html|css|scss|sh|bash|zsh|rb|go|rs|java|kt|swift|c|cc|cpp|h|hpp|cs|php|sql|ini|cfg|conf|lock|txt|csv|env)(?![\w])/i;
  var STEP_FILE_WORD_RE = /\b(?:read|reading|file|files)\b/i;
  var STEP_TOOL_WORD_RE =
    /\b(?:tool|tools|calling|invoke|invoking|write_note|send_suggestion|remember_fact|forget_fact|save_plan|pause_heartbeat|resume_heartbeat)\b/i;
  var STEP_REPRODUCE_WORD_RE =
    /\b(?:reproduce|reproducing|reproduction|crash|crashes|traceback|exception|failing|bug)\b/i;
  var STEP_TEST_WORD_RE =
    /\b(?:test|tests|testing|tested|pytest|unittest|verify|verifying|verification)\b/i;
  var STEP_PLAN_WORD_RE =
    /\b(?:plan|planning|approach|design|outline|trade-off|tradeoff)\b/i;
  var STEP_CHANGE_WORD_RE =
    /\b(?:implement|implementing|patch|patching|edit|editing|update|updating|fix|fixing|refactor|refactoring)\b/i;
  var STEP_REVIEW_WORD_RE = /\b(?:review|reviewing|diff|audit|auditing)\b/i;
  var STEP_REPO_WORD_RE =
    /\b(?:repository|repo|codebase|context|structure|directory|locate|locating|search|searching)\b/i;

  function describeThinkingStep(text) {
    var source = String(text || "");
    if (STEP_FILE_NAME_RE.test(source) || STEP_FILE_WORD_RE.test(source)) {
      return "Reading the file…";
    }
    if (STEP_TOOL_WORD_RE.test(source)) {
      return "Calling a tool…";
    }
    if (STEP_REPRODUCE_WORD_RE.test(source)) {
      return "Reproducing the failure…";
    }
    if (STEP_TEST_WORD_RE.test(source)) {
      return "Checking the tests…";
    }
    if (STEP_PLAN_WORD_RE.test(source)) {
      return "Planning the change…";
    }
    if (STEP_CHANGE_WORD_RE.test(source)) {
      return "Drafting the change…";
    }
    if (STEP_REVIEW_WORD_RE.test(source)) {
      return "Reviewing the code…";
    }
    if (STEP_REPO_WORD_RE.test(source)) {
      return "Checking the repository…";
    }
    return "Working through it…";
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
    maybeScrollToBottom(log);
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

  // Send a suggestion: a single text box in the sidebar that proposes
  // send_suggestion through the same confirmation gate and audit trail as
  // write_note. Nothing is written to suggestions.txt until Approve is clicked.
  function wireSuggestions() {
    var box = document.getElementById("suggestionBox");
    var input = document.getElementById("suggestionInput");
    var submit = document.getElementById("suggestionSubmit");
    var feedback = document.getElementById("suggestionFeedback");
    var cardHost = document.getElementById("suggestionCardHost");
    var toggle = document.getElementById("suggestionToggle");
    var sidebar = document.getElementById("sidebar");
    if (!input || !submit || input.dataset.orbitWired === "true") return;
    input.dataset.orbitWired = "true";

    function showFeedback(text, isError) {
      if (!feedback) return;
      feedback.textContent = text || "";
      feedback.hidden = !text;
      feedback.classList.toggle("error", !!isError);
    }

    if (toggle) {
      toggle.addEventListener("click", function () {
        if (sidebar && sidebar.classList.contains("sidebar--collapsed")) {
          applySidebarCollapsed(sidebar, false);
          rememberSidebarCollapsed(false);
        }
        if (box) box.hidden = false;
        input.focus();
      });
    }

    function submitSuggestion() {
      var text = input.value.trim();
      if (!text) {
        showFeedback("Write a suggestion first.", true);
        input.focus();
        return;
      }
      submit.disabled = true;
      showFeedback("", false);
      fetch("/api/suggestions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content: text }),
      })
        .then(function (res) {
          return res.json().catch(function () { return {}; }).then(function (data) {
            if (!res.ok) throw new Error(data.error || ("HTTP " + res.status));
            return data;
          });
        })
        .then(function (card) {
          input.value = "";
          if (!cardHost) return;
          cardHost.textContent = "";
          cardHost.hidden = false;
          var container = el("div", "action-card");
          container.setAttribute("data-action-id", card.id);
          cardHost.appendChild(container);
          renderActionCard(container, card);
        })
        .catch(function (err) {
          showFeedback((err && err.message) || "Could not queue that suggestion.", true);
        })
        .finally(function () {
          submit.disabled = false;
        });
    }

    submit.addEventListener("click", submitSuggestion);
    input.addEventListener("keydown", function (event) {
      if (event.key === "Enter") {
        event.preventDefault();
        submitSuggestion();
      }
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

  // The server keeps `error` short and stable and puts the specific cause in
  // `reason` (model not downloaded, huggingface.co unreachable, audio decoder
  // missing). Show both: a voice failure should be diagnosable from the status
  // line, not only from the server log.
  function voiceFailureText(payload, fallback) {
    var message = (payload && payload.error) || fallback;
    var reason = payload && payload.reason;
    if (!reason || reason === message) return message;
    return message + " " + reason;
  }

  function recordingExtension(mimeType) {
    var mime = (mimeType || "").toLowerCase();
    if (mime.indexOf("ogg") !== -1) return "ogg";
    if (mime.indexOf("mp4") !== -1 || mime.indexOf("m4a") !== -1) return "mp4";
    if (mime.indexOf("wav") !== -1) return "wav";
    if (mime.indexOf("mpeg") !== -1 || mime.indexOf("mp3") !== -1) return "mp3";
    return "webm";
  }

  // Tap once to start recording; tap a second time to stop, transcribe the
  // clip, and hand plain text back to the same send handler used by typing.
  function wirePushToTalk(button, statusNode, onTranscript, isBusy, onState) {
    if (!button) return;
    var supported = !!(
      navigator.mediaDevices && navigator.mediaDevices.getUserMedia &&
      typeof window.MediaRecorder !== "undefined"
    );
    var recorder = null;
    var stream = null;
    var chunks = [];
    var starting = false;
    var recording = false;
    var processing = false;
    var maxDurationTimer = null;

    function reportState(state, message, isError) {
      setVoiceStatus(statusNode, message, isError);
      if (typeof onState === "function") onState(state, message || "", !!isError);
    }

    function setActive(active) {
      button.classList.toggle("recording", active);
      button.textContent = active ? "■" : "🎙";
      button.setAttribute("aria-pressed", active ? "true" : "false");
      button.setAttribute("aria-label", active ? "Stop recording and transcribe" : "Tap to talk");
      button.title = active ? "Tap to stop" : "Tap to talk";
    }

    function stopTracks(mediaStream) {
      if (mediaStream) {
        mediaStream.getTracks().forEach(function (track) { track.stop(); });
      }
    }

    function updateDisabled() {
      button.disabled = !supported || processing || (!recording && !starting && !!(isBusy && isBusy()));
    }

    if (!supported) {
      button.disabled = true;
      reportState("idle", "Voice recording needs a browser with microphone support over HTTPS or localhost.", true);
      return function () {};
    }

    async function startRecording() {
      if (recording || starting || processing || (isBusy && isBusy())) return;
      starting = true;
      setActive(true);
      reportState("thinking", "Requesting microphone access…");
      try {
        var mediaStream = await navigator.mediaDevices.getUserMedia({ audio: true });
        if (!starting) {
          stopTracks(mediaStream);
          setActive(false);
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
            reportState("idle", "The recording was empty. Tap the mic button to start and tap again to send.", true);
            updateDisabled();
            return;
          }
          processing = true;
          updateDisabled();
          reportState("thinking", "Transcribing your message…");
          var form = new FormData();
          form.append("audio", blob, "recording." + recordingExtension(blob.type));
          fetch("/api/voice/transcribe", { method: "POST", body: form })
            .then(function (response) {
              return response.json().then(function (payload) {
                if (!response.ok) throw new Error(voiceFailureText(payload, "Transcription failed."));
                return payload;
              });
            })
            .then(function (payload) {
              var transcript = String(payload.transcript || "").trim();
              if (!transcript) throw new Error("No speech was detected. Try again.");
              reportState("thinking", "Sending transcript…");
              if (onTranscript) onTranscript(transcript);
            })
            .catch(function (error) {
              reportState("idle", error.message || "Could not transcribe that recording.", true);
            })
            .finally(function () {
              processing = false;
              updateDisabled();
            });
        });
        activeRecorder.addEventListener("error", function () {
          reportState("idle", "The microphone recording failed. Please try again.", true);
        });
        activeRecorder.start();
        starting = false;
        recording = true;
        setActive(true);
        reportState("listening", "Recording… tap to stop and send.");
        maxDurationTimer = window.setTimeout(function () {
          if (recording) {
            reportState("thinking", "Recording limit reached; transcribing the clip.");
            stopRecording();
          }
        }, 120000);
      } catch (error) {
        starting = false;
        recording = false;
        stopTracks(stream);
        stream = null;
        recorder = null;
        setActive(false);
        reportState("idle", error.message || "Microphone permission was denied.", true);
        updateDisabled();
      }
    }

    function stopRecording() {
      if (!recording && !starting) return;
      starting = false;
      recording = false;
      if (maxDurationTimer) window.clearTimeout(maxDurationTimer);
      maxDurationTimer = null;
      setActive(false);
      if (recorder && recorder.state === "recording") {
        reportState("thinking", "Transcribing your message…");
        recorder.stop();
      } else {
        stopTracks(stream);
        stream = null;
        recorder = null;
        reportState("idle", "");
      }
    }

    function toggleRecording(event) {
      if (event && event.preventDefault) event.preventDefault();
      if (recording || starting) {
        stopRecording();
      } else {
        startRecording();
      }
    }

    button.addEventListener("click", toggleRecording);
    document.addEventListener("visibilitychange", function () {
      if (document.hidden && (recording || starting)) stopRecording();
    });
    updateDisabled();
    return stopRecording;
  }

  function showAudioError(body, error, onPlaybackState) {
    var detail = error && error.message ? error.message : "could not synthesize speech";
    showLine(body, "Spoken reply unavailable: " + detail, "warn");
    if (typeof onPlaybackState === "function") {
      onPlaybackState("idle", "Spoken reply unavailable: " + detail, true);
    }
  }

  function playReplyAudio(text, body, onPlaybackState) {
    function reportPlayback(state, message, isError) {
      if (typeof onPlaybackState === "function") onPlaybackState(state, message || "", !!isError);
    }

    function failAudio(error) {
      showAudioError(body, error, reportPlayback);
    }

    function startPlayback(audio) {
      var playback;
      try {
        playback = audio.play();
      } catch (_error) {
        reportPlayback("idle", "Reply ready. Return to text chat to play it.", false);
        return;
      }
      if (playback && playback.catch) {
        playback.catch(function () {
          reportPlayback("idle", "Reply ready. Return to text chat to play it.", false);
        });
      }
    }

    reportPlayback("thinking", "Preparing spoken reply…", false);
    fetch("/api/voice/speech", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: text }),
    })
      .then(function (response) {
        if (!response.ok) {
          return response.json().then(function (payload) {
            throw new Error(voiceFailureText(payload, "Speech synthesis failed."));
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
        audio.addEventListener("playing", function () {
          reportPlayback("speaking", "Speaking…", false);
        });
        audio.addEventListener("pause", function () {
          if (!audio.ended) reportPlayback("idle", "Reply paused.", false);
        });
        audio.addEventListener("ended", function () {
          reportPlayback("idle", "Ready when you are", false);
        });
        audio.addEventListener("error", function () {
          failAudio(new Error("the audio stream could not be played"));
        });
        body.appendChild(audio);

        var canStream = !!(
          window.MediaSource && window.MediaSource.isTypeSupported &&
          window.MediaSource.isTypeSupported("audio/mpeg") && response.body && response.body.getReader
        );
        if (!canStream) {
          return response.blob().then(function (blob) {
            audio.src = URL.createObjectURL(blob);
            startPlayback(audio);
          });
        }

        var mediaSource = new window.MediaSource();
        audio.src = URL.createObjectURL(mediaSource);
        mediaSource.addEventListener("sourceopen", function () {
          var sourceBuffer;
          try {
            sourceBuffer = mediaSource.addSourceBuffer("audio/mpeg");
          } catch (error) {
            failAudio(error);
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
                failAudio(error);
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
              failAudio(error);
              if (mediaSource.readyState === "open") mediaSource.endOfStream("decode");
            });
          }
          sourceBuffer.addEventListener("updateend", function () {
            startPlayback(audio);
            appendNext();
          });
          appendNext();
        }, { once: true });
      })
      .catch(function (error) { failAudio(error); });
  }

  // --- HOME --------------------------------------------------------------
  function bootstrapHome() {
    wireSidebarCollapse();
    wireModeToggles();
    wireThinkingToggles();
    wireNewConversation();
    wireSettingsControls();
    wireReminders();
    wireSuggestions();
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
    var voiceExpand = document.getElementById("homeVoiceExpand");
    var voiceChatView = document.getElementById("voiceChatView");
    var voiceChatBack = document.getElementById("voiceChatBack");
    var voiceViewMic = document.getElementById("voiceViewMic");
    var voiceViewRings = document.getElementById("voiceViewRings");
    var voiceViewState = document.getElementById("voiceViewState");
    var voiceViewTranscript = document.getElementById("voiceViewTranscript");
    var voiceViewReply = document.getElementById("voiceViewReply");
    var voiceHomeParent = voiceButton ? voiceButton.parentNode : null;
    var voiceHomeNextSibling = voiceButton ? voiceButton.nextSibling : null;
    var releaseVoiceChat = null;
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
      if (!welcome || welcome.hidden) {
        if (turnStatus.bar) turnStatus.bar.classList.remove("no-rings");
        return;
      }
      welcome.classList.add("is-responding");
      if (turnStatus.bar) turnStatus.bar.classList.add("no-rings");
    }

    function welcomeDone() {
      if (welcome) welcome.hidden = true;
      if (turnStatus.bar) turnStatus.bar.classList.remove("no-rings");
    }

    // The turn did not land, so the welcome screen goes back to idle with its
    // shortcuts available rather than staying half-collapsed.
    function welcomeIdle() {
      if (welcome && !thread.querySelector(".chat-row")) {
        welcome.hidden = false;
        welcome.classList.remove("is-responding");
      }
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

    function setVoiceViewState(state, message, isError) {
      if (voiceViewRings) voiceViewRings.setAttribute("data-state", state || "idle");
      if (voiceViewState) {
        voiceViewState.textContent = message || (state === "idle" ? "Ready when you are" : "");
        voiceViewState.classList.toggle("error", !!isError);
      }
    }

    function openVoiceChat() {
      if (!voiceChatView) return;
      voiceChatView.hidden = false;
      if (voiceExpand) voiceExpand.setAttribute("aria-expanded", "true");
      if (voiceButton && voiceViewMic && voiceButton.parentNode !== voiceViewMic) {
        voiceViewMic.appendChild(voiceButton);
      }
      if (voiceChatBack) voiceChatBack.focus();
    }

    function closeVoiceChat() {
      if (!voiceChatView) return;
      if (releaseVoiceChat) releaseVoiceChat();
      if (voiceButton && voiceHomeParent) {
        if (voiceHomeNextSibling && voiceHomeNextSibling.parentNode === voiceHomeParent) {
          voiceHomeParent.insertBefore(voiceButton, voiceHomeNextSibling);
        } else {
          voiceHomeParent.appendChild(voiceButton);
        }
      }
      voiceChatView.hidden = true;
      if (voiceExpand) voiceExpand.setAttribute("aria-expanded", "false");
      if (voiceExpand && !voiceExpand.disabled) voiceExpand.focus();
      else if (input) input.focus();
    }

    setVoiceViewState("idle", "Ready when you are", false);
    if (voiceExpand) voiceExpand.addEventListener("click", openVoiceChat);
    if (voiceChatBack) voiceChatBack.addEventListener("click", closeVoiceChat);
    if (voiceChatView) {
      voiceChatView.addEventListener("keydown", function (event) {
        if (event.key === "Escape") {
          closeVoiceChat();
          return;
        }
        if (event.key !== "Tab") return;
        var focusable = voiceChatView.querySelectorAll("button:not([disabled]), [href], input:not([disabled]), [tabindex]:not([tabindex='-1'])");
        if (!focusable.length) return;
        var first = focusable[0];
        var last = focusable[focusable.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      });
    }
    wireMessageActions(thread, regenerateHome);
    var homeScroller = createAutoScroller(thread);
    formatExistingReplies(thread);

    function startStream(row, body, reply, payload, voiceReply) {
      inflight = true;
      activeRequest = window.AbortController ? new window.AbortController() : null;
      var requestController = activeRequest;
      setSendButton(send, true);
      if (voiceButton) voiceButton.disabled = true;
      if (voiceExpand) voiceExpand.disabled = true;
      setVoiceStatus(voiceStatus, "");
      if (voiceReply) setVoiceViewState("thinking", "Thinking…", false);
      if (payload && payload.message) {
        row.setAttribute("data-retry-message", payload.message);
      }
      clearRetryUI(row);
      row.classList.add("is-generating");
      row.classList.remove("is-streaming");
      body.classList.remove("streaming");
      reply.classList.remove("streaming");
      reply.classList.remove("in-code-block");
      refreshRegenerateActions(thread);
      var fullReply = "";
      var sawAnswer = false;
      var sawAction = false;
      var sawError = false;
      turnStatus.placeBefore(row);
      turnStatus.begin();
      welcomeResponding();
      homeScroller.scrollToBottomIfPinned();

      postSSE(
        "/api/chat",
        payload,
        function (event, data) {
          if (event === "status") {
            if (!sawAnswer && data) turnStatus.arriving(data);
          } else if (event === "thinking") {
            if (!sawAnswer) turnStatus.arriving(describeThinkingStep(data));
          } else if (event === "answer") {
            if (!sawAnswer) {
              sawAnswer = true;
              // The first chunk is on screen, so the label stops saying we are
              // waiting and starts saying what is arriving; the reply row now
              // replaces the thinking bar in place with its own inline cursor.
              turnStatus.arriving("Writing your answer…");
              turnStatus.clear();
              row.classList.remove("is-generating");
              row.classList.add("is-streaming");
              body.classList.add("streaming");
              reply.classList.add("streaming");
            }
            fullReply += data;
            renderReplyContent(reply, fullReply);
            if (voiceReply && voiceViewReply) {
              voiceViewReply.hidden = false;
              voiceViewReply.textContent += data;
            }
            homeScroller.scrollToBottomIfPinned();
          } else if (event === "conversation") {
            var created = JSON.parse(data);
            addHistoryItem(created.id, created.title);
          } else if (event === "title") {
            var named = JSON.parse(data);
            setHistoryTitle(named.id, named.title);
          } else if (event === "action") {
            sawAction = true;
            if (!sawAnswer) turnStatus.arriving("Calling a tool…");
            appendActionCard(thread, JSON.parse(data));
            refreshRegenerateActions(thread);
          } else if (event === "followups") {
            renderFollowups(followupsBox, JSON.parse(data));
          } else if (event === "notice") {
            row.classList.remove("is-generating");
            showLine(body, data, "warn");
          } else if (event === "message") {
            // The stored reply's id: this row's Copy/Regenerate/rating buttons
            // act on it from here on.
            try {
              setMessageId(row, JSON.parse(data).id);
            } catch (_error) {
              // A malformed id line must not break the reply that just arrived.
            }
          } else if (event === "done") {
            row.classList.remove("is-generating", "is-streaming");
            reply.classList.remove("streaming");
            reply.classList.remove("in-code-block");
            body.classList.remove("streaming");
            turnStatus.clear();
          } else if (event === "error") {
            sawError = true;
            row.classList.remove("is-generating", "is-streaming");
            row.classList.add("has-error");
            reply.classList.remove("streaming");
            reply.classList.remove("in-code-block");
            body.classList.remove("streaming");
            turnStatus.clear();
            showLine(body, data, "error");
            attachRetryAction(row, body, !sawAnswer);
          }
        },
        function (err) {
          var stopped = !!(requestController && requestController.signal.aborted);
          inflight = false;
          activeRequest = null;
          setSendButton(send, false);
          if (voiceButton) voiceButton.disabled = false;
          if (voiceExpand) voiceExpand.disabled = false;
          turnStatus.clear();
          row.classList.remove("is-generating", "is-streaming");
          row.classList.toggle("was-stopped", stopped);
          reply.classList.remove("streaming");
          reply.classList.remove("in-code-block");
          body.classList.remove("streaming");
          if (err) {
            sawError = true;
            row.classList.add("has-error");
            showLine(body, err, "error");
            attachRetryAction(row, body, !sawAnswer);
            welcomeIdle();
          } else if (stopped) {
            if (!body.querySelector(".notice")) {
              showLine(body, "Response was stopped before finishing.", "warn");
            }
            attachRetryAction(row, body, !sawAnswer);
            welcomeDone();
          } else if (sawError) {
            attachRetryAction(row, body, !sawAnswer);
            welcomeDone();
          } else {
            if (!fullReply.trim() && sawAction && !body.querySelector(".notice") && row.parentNode) {
              row.parentNode.removeChild(row);
            }
            // Answered or stopped: the conversation has started, so the welcome
            // screen has done its job and steps out.
            welcomeDone();
          }
          if (voiceReply) {
            if (err) {
              setVoiceViewState("idle", err, true);
            } else if (!stopped && fullReply.trim()) {
              playReplyAudio(fullReply, body, setVoiceViewState);
            } else {
              setVoiceViewState("idle", stopped ? "Reply stopped." : "No spoken reply was produced.", !stopped);
            }
          }
          refreshRegenerateActions(thread);
        },
        activeRequest ? activeRequest.signal : undefined
      );
    }

    // Regenerating rewrites one reply where it stands, so any Orbit reply can be
    // redone, not only the newest. The row's id names the message to replace; if
    // the message failed or was cut off before receiving an id, it retries in place.
    function regenerateHome(row) {
      if (inflight || !row || !row.classList.contains("orbit")) return;
      var body = row.querySelector(".body");
      var reply = row.querySelector(".reply");
      if (!body || !reply) return;
      var id = messageId(row);
      var retryText = row.getAttribute("data-retry-message") || prevUserRowText(row);
      var canRetryFailed =
        row.getAttribute("data-can-retry") === "true" ||
        row.classList.contains("has-error") ||
        row.classList.contains("was-stopped");
      if (!canRetryFailed && !messageText(row).trim()) return;
      if (!id && !canRetryFailed) {
        // No id means there is nothing stored to rewrite. Say so rather than
        // leaving a button that looks live and does nothing.
        showLine(body, "This reply was not stored with an id, so it cannot be regenerated.", "warn");
        return;
      }
      homeScroller.captureBeforeChange();
      Array.prototype.forEach.call(body.querySelectorAll(".notice"), function (notice) { notice.remove(); });
      clearRetryUI(row);
      renderReplyContent(reply, "");
      renderFollowups(followupsBox, []);
      var payload = id
        ? { regenerate: true, message_id: id }
        : { retry: true, message: retryText };
      startStream(row, body, reply, payload, false);
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
      welcomeDone();
      dockComposer();
      thread.classList.add("visible");
      homeScroller.captureBeforeChange();
      var userRow = makeUserBubble(thread, text);
      turnStatus.placeAfter(userRow);
      var orbitMessage = makeRow(thread, "orbit");
      orbitMessage.row._userRow = userRow;
      userRow._orbitRow = orbitMessage.row;
      orbitMessage.row.setAttribute("data-retry-message", text);
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
    releaseVoiceChat = wirePushToTalk(voiceButton, voiceStatus, function (transcript) {
      if (voiceViewTranscript) {
        voiceViewTranscript.textContent = "You: " + transcript;
        voiceViewTranscript.hidden = false;
      }
      if (voiceViewReply) {
        voiceViewReply.textContent = "";
        voiceViewReply.hidden = true;
      }
      setVoiceViewState("thinking", "Thinking…", false);
      input.value = transcript;
      sendMessage({ voiceReply: true });
    }, function () { return inflight; }, function (state, message, isError) {
      setVoiceViewState(state, message, isError);
    });
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
        (state.running ? " is-running" : state.status === "failed" ? " is-failed" : " is-idle");
      node.textContent = "";
      node.appendChild(el("span", "dot"));
      node.appendChild(el("span", "op-text", escapeHtml(state.operation || "Not running")));
    }
    // The topbar indicator only exists while a run is genuinely in flight.
    var indicator = document.getElementById("workingIndicator");
    if (indicator) {
      indicator.hidden = !state.running;
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
    // A planned run is not an in-flight one, so "in progress" waits for the stream.
    setText("taskRunState", !state.started ? "NOT STARTED" : RUN_LABEL[state.status] || "WAITING FOR YOU");
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
    wireSidebarCollapse();
    wireThinkingToggles();
    wireNewConversation();
    wireSettingsControls();
    wireReminders();
    wireSuggestions();
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
    var taskScroller = createAutoScroller(log);
    wireMessageActions(log, regenerateTask);
    formatExistingReplies(log);

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
      taskScroller.captureBeforeChange();
      var orbit = makeOrbitBubble(log, opts.replaceRow || null);
      if (opts.userRow) {
        orbit.row._userRow = opts.userRow;
        opts.userRow._orbitRow = orbit.row;
      }
      if (opts.message) orbit.row.setAttribute("data-retry-message", opts.message);
      if (opts.initial) orbit.row.setAttribute("data-retry-initial", "true");
      clearRetryUI(orbit.row);
      var fullReply = "";
      var sawAnswer = false;
      var sawError = false;
      var thinkingBuffer = "";
      var targetThink = useTop ? thinkText : orbit.thinkText;
      var targetBlock = useTop ? thinkBlock : orbit.think;
      var targetLabel = useTop ? thinkLabel : orbit.thinkLabel;
      if (!useTop && targetBlock) targetBlock.style.display = "none";
      if (useTop && targetBlock) {
        targetBlock.classList.remove("done");
        if (targetThink && (opts.retry || opts.regenerate)) targetThink.textContent = "";
      }
      orbit.row.classList.add("is-generating");
      orbit.row.classList.remove("is-streaming", "has-thinking");
      orbit.body.classList.remove("streaming");
      orbit.reply.classList.remove("streaming");
      orbit.reply.classList.remove("in-code-block");
      // The panel has state to show as soon as the run exists.
      hasRun = true;
      if (startBox) startBox.hidden = true;
      var panelNode = document.getElementById("taskPanel");
      if (panelNode) panelNode.hidden = false;
      turnStatus.placeBefore(orbit.row);
      turnStatus.begin();
      refreshRegenerateActions(log);
      taskScroller.scrollToBottomIfPinned();

      function revealThinking() {
        if (!useTop && targetBlock) {
          orbit.row.classList.add("has-thinking");
          targetBlock.style.display = "";
        }
      }

      postSSE(
        "/api/task/" + encodeURIComponent(taskId) + "/stream",
        {
          message: opts.message || "",
          initial: !!opts.initial,
          regenerate: !!opts.regenerate,
          retry: !!opts.retry,
          message_id: opts.messageId || "",
        },
        function (event, data) {
          if (event === "task_state") {
            // Steps, files, the current operation, approvals and the activity
            // log — all of it derived from this run, never from seeded numbers.
            var state = JSON.parse(data);
            renderTaskPanel(state);
            if (!sawAnswer && state && state.running && state.operation && state.operation !== "Thinking…") {
              turnStatus.arriving(state.operation);
            }
          } else if (event === "status") {
            showModelLabel(targetLabel, data);
            if (!sawAnswer && data) turnStatus.arriving(data);
          } else if (event === "thinking") {
            thinkingBuffer += data;
            if (!sawAnswer) {
              turnStatus.arriving(describeThinkingStep(data || thinkingBuffer));
            }
            if (!targetThink) return;
            revealThinking();
            targetThink.textContent += data;
            taskScroller.scrollToBottomIfPinned();
          } else if (event === "action") {
            if (!sawAnswer) turnStatus.arriving("Calling a tool…");
          } else if (event === "answer") {
            if (!sawAnswer) {
              sawAnswer = true;
              turnStatus.arriving("Writing your answer…");
              turnStatus.clear();
              orbit.row.classList.remove("is-generating");
              orbit.row.classList.add("is-streaming");
              orbit.body.classList.add("streaming");
              orbit.reply.classList.add("streaming");
            }
            if (targetBlock && !targetBlock.classList.contains("done")) targetBlock.classList.add("done");
            fullReply += data;
            renderReplyContent(orbit.reply, fullReply);
            taskScroller.scrollToBottomIfPinned();
          } else if (event === "message") {
            // The stored reply's id, so this row's actions name a real message.
            try {
              setMessageId(orbit.row, JSON.parse(data).id);
            } catch (_error) {
              // A malformed id line must not break the reply that just arrived.
            }
          } else if (event === "done") {
            if (targetBlock) targetBlock.classList.add("done");
            orbit.row.classList.remove("is-generating", "is-streaming");
            orbit.reply.classList.remove("streaming");
            orbit.reply.classList.remove("in-code-block");
            orbit.body.classList.remove("streaming");
            turnStatus.clear();
          } else if (event === "error") {
            sawError = true;
            if (targetBlock) targetBlock.classList.add("done");
            orbit.row.classList.remove("is-generating", "is-streaming");
            orbit.row.classList.add("has-error");
            orbit.reply.classList.remove("streaming");
            orbit.reply.classList.remove("in-code-block");
            orbit.body.classList.remove("streaming");
            turnStatus.clear();
            showLine(orbit.body, data, "error");
            attachRetryAction(orbit.row, orbit.body, !sawAnswer);
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
          orbit.reply.classList.remove("streaming");
          orbit.reply.classList.remove("in-code-block");
          orbit.body.classList.remove("streaming");
          if (err) {
            orbit.row.classList.add("has-error");
            showLine(orbit.body, err, "error");
            attachRetryAction(orbit.row, orbit.body, !sawAnswer);
          } else if (stopped) {
            if (!orbit.body.querySelector(".notice")) {
              showLine(orbit.body, "Response was stopped before finishing.", "warn");
            }
            attachRetryAction(orbit.row, orbit.body, !sawAnswer);
          } else if (sawError) {
            attachRetryAction(orbit.row, orbit.body, !sawAnswer);
          } else if (opts.voiceReply && fullReply.trim()) {
            playReplyAudio(fullReply, orbit.body);
          }
          refreshRegenerateActions(log);
        },
        requestController ? requestController.signal : undefined
      );
    }

    // Any task reply can be redone; the row's id names the message to replace,
    // or if the turn failed/stopped before receiving an id it retries in place.
    function regenerateTask(row) {
      if (inflight || !row || !row.classList.contains("orbit")) return;
      var reply = row.querySelector(".reply");
      if (!reply) return;
      var id = messageId(row);
      var retryText = row.getAttribute("data-retry-message") || prevUserRowText(row);
      var retryInitial = row.getAttribute("data-retry-initial") === "true";
      var canRetryFailed =
        row.getAttribute("data-can-retry") === "true" ||
        row.classList.contains("has-error") ||
        row.classList.contains("was-stopped");
      if (!canRetryFailed && !messageText(row).trim()) return;
      if (!id && !canRetryFailed) {
        // Same honesty as the home screen: a control that cannot act says so.
        showLine(
          row.querySelector(".body") || row,
          "This reply was not stored with an id, so it cannot be regenerated.",
          "warn"
        );
        return;
      }
      if (id) {
        streamTurn({
          regenerate: true,
          replaceRow: row,
          useTopThinking: false,
          messageId: id,
        });
      } else {
        streamTurn({
          retry: true,
          initial: retryInitial,
          message: retryText,
          replaceRow: row,
          useTopThinking: retryInitial,
        });
      }
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
        taskScroller.captureBeforeChange();
        var userRow = makeUserBubble(log, text);
        turnStatus.placeAfter(userRow);
        streamTurn({
          message: text,
          userRow: userRow,
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
      // Nothing is streaming into the top block, so mark it done rather than
      // leaving the orbital mark spinning under the header.
      thinkBlock.classList.add("done");
    }

    // The approval gate lives in the store, not in a button: app.store.apply_changes
    // refuses unless the task was explicitly approved, and /api/task/<id>/apply
    // answers 403. There is deliberately no Approve control here — with nothing
    // proposed there is nothing to approve, and a button that only looks like a
    // gate is worse than the honest line the panel shows instead.
  }

  function bootstrapSidebar() {
    wireSidebarCollapse();
    wireSettingsControls();
    wireReminders();
    wireSuggestions();
    wireConversationSearch();
  }

  window.Orbit = {
    bootstrapHome: bootstrapHome,
    bootstrapTask: bootstrapTask,
    // The Analytics view has no chat to wire; it wires the sidebar and suggestion box.
    bootstrapSidebar: bootstrapSidebar,
  };
})();
