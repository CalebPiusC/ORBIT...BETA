/* orbit — agent conversation client (conversation.js)
 *
 * Drives one specialist's conversation with one project on
 * /projects/<id>/agents/<agent>: the composer posts to the workspace's own
 * /api/project/<id>/stream (agent fixed to this page's specialty) and folds
 * the SSE events into the thread. The rules mirror app.js and project.js
 * (handoff §1.8): model text and chips are built as DOM nodes (never
 * innerHTML), every turn ends visibly (a final answer, or an error line
 * naming what happened), and the scroll only follows the stream when the
 * person is already at the bottom.
 *
 * The thread follows the chat rules: the person's messages sit right, the
 * agent's left, and no row carries a name label — the page heading already
 * says who is speaking.
 */
(function () {
  "use strict";

  const composer = document.getElementById("cvComposer");
  const input = document.getElementById("cvInput");
  const sendBtn = document.getElementById("cvSend");
  const thread = document.getElementById("cvThread");
  const turnStatus = document.getElementById("cvTurnStatus");
  const turnStatusText = document.getElementById("cvTurnStatusText");
  const followupsRow = document.getElementById("cvFollowups");
  const modeZone = document.getElementById("cvModeZone");
  if (!composer || !input || !thread) return;

  const projectId = composer.getAttribute("data-project-id");
  const agentId = composer.getAttribute("data-agent") || "main";
  // The task the conversation is focused on right now (server-rendered from
  // the open Task Details), so the server can join its real state.
  let streaming = false;

  function nearBottom() {
    return thread.scrollTop + thread.clientHeight >= thread.scrollHeight - 60;
  }

  function maybeScroll(stick) {
    if (stick) thread.scrollTop = thread.scrollHeight;
  }

  function addRow(who) {
    const row = document.createElement("div");
    row.className = "cv-msg " + (who === "orbit" ? "cv-msg--orbit" : "cv-msg--user");
    const bubble = document.createElement("div");
    bubble.className = "cv-bubble";
    row.appendChild(bubble);
    thread.appendChild(row);
    return row;
  }

  function setStatus(text) {
    if (!turnStatus) return;
    if (text) {
      turnStatusText.textContent = text;
      turnStatus.hidden = false;
    } else {
      turnStatus.hidden = true;
    }
  }

  function clearEmptyState() {
    const empty = thread.querySelector(".cv-thread__empty");
    if (empty) empty.remove();
    // The day label belongs to a thread with messages; add it with the first.
    if (!thread.querySelector(".cv-day")) {
      const day = document.createElement("p");
      day.className = "cv-day";
      day.textContent = "Today, " + new Date().toTimeString().slice(0, 5);
      thread.insertBefore(day, thread.firstChild);
    }
  }

  function renderFollowups(chips) {
    if (!followupsRow) return;
    followupsRow.textContent = "";
    (chips || []).forEach((chip) => {
      if (!chip || !chip.prompt || !chip.label) return;
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "cv-chip";
      btn.setAttribute("data-prompt", chip.prompt);
      btn.textContent = chip.label;
      followupsRow.appendChild(btn);
    });
  }

  /* Minimal, honest code formatting: fenced blocks become <pre> with a hover
     copy button; nothing claims syntax highlighting it does not do. */
  function formatReply(bubble, rawText) {
    bubble.textContent = "";
    const parts = rawText.split(/```(?:([\w+-]*)\n)?/);
    for (let i = 0; i < parts.length; i++) {
      if (i % 3 === 1) continue; // the language label of a block (unused)
      if (i % 3 === 2) {
        const pre = document.createElement("pre");
        const code = document.createElement("code");
        code.textContent = parts[i].replace(/\n$/, "");
        const copy = document.createElement("button");
        copy.type = "button";
        copy.className = "cv-code-copy";
        copy.textContent = "Copy";
        copy.addEventListener("click", () => {
          navigator.clipboard.writeText(code.textContent).then(() => {
            copy.textContent = "Copied";
            setTimeout(() => (copy.textContent = "Copy"), 1500);
          });
        });
        pre.appendChild(copy);
        pre.appendChild(code);
        bubble.appendChild(pre);
      } else if (parts[i]) {
        bubble.appendChild(document.createTextNode(parts[i]));
      }
    }
  }

  async function sendPrompt(text) {
    const message = (text || "").trim();
    if (!message || streaming) return;
    streaming = true;
    sendBtn.disabled = true;
    input.disabled = true;
    clearEmptyState();
    if (followupsRow) followupsRow.textContent = "";
    const stick = nearBottom();
    const userRow = addRow("user");
    userRow.querySelector(".cv-bubble").textContent = message;
    const row = addRow("orbit");
    const bubble = row.querySelector(".cv-bubble");
    const reply = document.createElement("span");
    bubble.appendChild(reply);
    setStatus("Thinking…");
    maybeScroll(true);

    let raw = "";
    let thinkingBlock = null;
    let failed = false;

    try {
      const resp = await fetch("/api/project/" + encodeURIComponent(projectId) + "/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        // agent: which conversation this turn belongs to — fixed to the
        // specialty this page is about. No task pin lives here.
        body: JSON.stringify({ message, agent: agentId }),
      });
      if (!resp.ok || !(resp.headers.get("content-type") || "").includes("text/event-stream")) {
        let detail = "The turn could not start.";
        try {
          const data = await resp.json();
          if (data && data.error) detail = data.error;
        } catch (_e) { /* keep the generic line */ }
        throw new Error(detail);
      }
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let cut;
        while ((cut = buffer.indexOf("\n\n")) >= 0) {
          const block = buffer.slice(0, cut);
          buffer = buffer.slice(cut + 2);
          let event = "message";
          const dataLines = [];
          block.split("\n").forEach((line) => {
            if (line.startsWith("event:")) event = line.slice(6).trim();
            else if (line.startsWith("data:")) dataLines.push(line.slice(5).replace(/^ /, ""));
          });
          const data = dataLines.join("\n");
          const stickNow = nearBottom();
          if (event === "thinking") {
            if (!thinkingBlock) {
              thinkingBlock = document.createElement("pre");
              thinkingBlock.className = "cv-thinking";
              bubble.insertBefore(thinkingBlock, reply);
            }
            thinkingBlock.appendChild(document.createTextNode(data));
            setStatus("Working through it…");
          } else if (event === "status") {
            setStatus(data); // Agent-mode model attempt lines: "Asking Titan…"
          } else if (event === "answer") {
            if (!raw) setStatus("Writing your answer…");
            raw += data;
            reply.appendChild(document.createTextNode(data));
          } else if (event === "message") {
            try {
              const meta = JSON.parse(data);
              if (meta && meta.id) row.setAttribute("data-message-id", meta.id);
            } catch (_e) { /* ids are a nicety, not a failure */ }
          } else if (event === "followups") {
            try {
              renderFollowups(JSON.parse(data));
            } catch (_e) { /* malformed chips offer nothing rather than breaking */ }
          } else if (event === "error") {
            failed = true;
            const err = document.createElement("div");
            err.className = "cv-error-line";
            err.textContent = data || "Something went wrong on this turn.";
            bubble.appendChild(err);
          } else if (event === "done") {
            // finished below
          }
          maybeScroll(stickNow);
        }
      }
    } catch (err) {
      failed = true;
      const errLine = document.createElement("div");
      errLine.className = "cv-error-line";
      errLine.textContent = (err && err.message) || "The turn failed.";
      bubble.appendChild(errLine);
    } finally {
      if (raw) formatReply(bubble, raw);
      if (!raw && !failed) {
        // A silent end looks exactly like a hang; say so instead.
        const errLine = document.createElement("div");
        errLine.className = "cv-error-line";
        errLine.textContent = "The provider returned an empty reply.";
        bubble.appendChild(errLine);
      }
      setStatus(null);
      streaming = false;
      sendBtn.disabled = false;
      input.disabled = false;
      input.value = "";
      input.focus();
    }
  }

  composer.addEventListener("submit", (e) => {
    e.preventDefault();
    sendPrompt(input.value);
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendPrompt(input.value);
    }
  });
  document.addEventListener("click", (e) => {
    const chip = e.target.closest(".cv-chip");
    if (chip) sendPrompt(chip.getAttribute("data-prompt"));
  });

  /* Chat / Agent and Quick / Deep: two separate controls, same global
     settings as everywhere else. Rendered by JS (the toggles fetch JSON), so
     without JavaScript nothing inert shows up. */
  function renderModeZone(currentMode, currentThinking) {
    if (!modeZone) return;
    modeZone.textContent = "";
    const seg = document.createElement("span");
    seg.className = "cv-seg";
    seg.setAttribute("role", "group");
    seg.setAttribute("aria-label", "Workspace mode");
    ["chat", "agent"].forEach((m) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "cv-seg__opt";
      b.textContent = m === "chat" ? "Chat" : "Agent";
      b.setAttribute("aria-pressed", m === currentMode ? "true" : "false");
      b.title = m === currentMode
        ? "This is the current mode."
        : "Switch to " + (m === "chat" ? "Chat" : "Agent") + " mode everywhere in orbit.";
      b.disabled = m === currentMode;
      b.addEventListener("click", () => {
        fetch("/api/mode", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ mode: m }),
        })
          .then((r) => r.json().catch(() => null))
          .then((data) => renderModeZone(data && data.mode ? data.mode : m, currentThinking))
          .catch(() => {/* a failed toggle leaves the control as it was */});
      });
      seg.appendChild(b);
    });
    const think = document.createElement("button");
    think.type = "button";
    think.className = "cv-depth";
    think.textContent = currentThinking === "deep" ? "Deep" : "Quick";
    think.setAttribute("aria-pressed", currentThinking === "deep" ? "true" : "false");
    think.title = "Switch the Agent-mode narration depth (" +
      (currentThinking === "deep" ? "currently Deep" : "currently Quick") + ").";
    think.addEventListener("click", () => {
      const next = currentThinking === "deep" ? "quick" : "deep";
      fetch("/api/thinking-level", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ thinking_level: next }),
      })
        .then((r) => r.json().catch(() => null))
        .then((data) => renderModeZone(currentMode, data && data.thinking_level ? data.thinking_level : next))
        .catch(() => {/* leave as-is on failure */});
    });
    modeZone.appendChild(seg);
    modeZone.appendChild(think);
  }
  renderModeZone(composer.getAttribute("data-mode") || "chat",
                 composer.getAttribute("data-thinking") || "quick");

  window.OrbitConversation = { sendPrompt };
  thread.scrollTop = thread.scrollHeight;
})();
