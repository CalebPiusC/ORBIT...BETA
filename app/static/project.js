/* orbit — project workspace client (project.js)
 *
 * Drives the Main Agent conversation on /projects/<id>: the composer posts to
 * /api/project/<id>/stream and folds the SSE events into the thread. The rules
 * mirror app.js (fromdocs/handoff §1.8): model text and chips are built as DOM
 * nodes (never innerHTML), every turn ends visibly (a final answer, or an
 * error line naming what happened), and the scroll only follows the stream
 * when the person is already at the bottom.
 */
(function () {
  "use strict";

  const composer = document.getElementById("wsComposer");
  const input = document.getElementById("wsInput");
  const sendBtn = document.getElementById("wsSend");
  const thread = document.getElementById("wsThread");
  const turnStatus = document.getElementById("wsTurnStatus");
  const turnStatusText = document.getElementById("wsTurnStatusText");
  const followupsRow = document.getElementById("wsFollowups");
  if (!composer || !input || !thread) return;

  const projectId = composer.getAttribute("data-project-id");
  const agentId = composer.getAttribute("data-agent") || "main";
  const speaker = composer.getAttribute("data-speaker") || "Main Agent";
  const taskPin = document.getElementById("wsTaskPin");
  const modeZone = document.getElementById("wsModeZone");
  // The task the conversation is pinned to right now. Initial value is the
  // server-rendered "Now discussing" task (when the page opened on one).
  const pinned = { taskId: composer.getAttribute("data-task-id") || "" };
  if (taskPin && taskPin.value && !pinned.taskId) pinned.taskId = taskPin.value;
  let streaming = false;

  function nearBottom() {
    return thread.scrollTop + thread.clientHeight >= thread.scrollHeight - 60;
  }

  function maybeScroll(stick) {
    if (stick) thread.scrollTop = thread.scrollHeight;
  }

  function addRow(who, text) {
    const row = document.createElement("div");
    row.className = "chat-row " + who;
    if (who === "orbit") {
      const label = document.createElement("div");
      label.className = "chat-speaker";
      label.textContent = speaker;
      row.appendChild(label);
    }
    const bubble = document.createElement("div");
    bubble.className = "chat-bubble " + who;
    const body = document.createElement("div");
    body.className = "body";
    if (who === "orbit") {
      const icon = document.createElement("img");
      icon.className = "orbit-icon";
      icon.src = document.querySelector(".brand-mark").src;
      icon.alt = "";
      body.appendChild(icon);
      const reply = document.createElement("span");
      reply.className = "reply";
      body.appendChild(reply);
    } else {
      body.textContent = text;
    }
    bubble.appendChild(body);
    row.appendChild(bubble);
    thread.appendChild(row);
    return row;
  }

  function orbitParts(row) {
    return {
      body: row.querySelector(".body"),
      reply: row.querySelector(".reply"),
    };
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
    const empty = thread.querySelector(".ws-thread-empty");
    if (empty) empty.remove();
  }

  function renderFollowups(chips) {
    followupsRow.textContent = "";
    (chips || []).forEach((chip) => {
      if (!chip || !chip.prompt || !chip.label) return;
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "ws-chip";
      btn.setAttribute("data-prompt", chip.prompt);
      btn.textContent = chip.label;
      followupsRow.appendChild(btn);
    });
  }

  /* Minimal, honest code formatting: fenced blocks become <pre> with a hover
     copy button; nothing claims syntax highlighting it does not do. */
  function formatReply(span, rawText) {
    span.textContent = "";
    const parts = rawText.split(/```(?:([\w+-]*)\n)?/);
    // With one capture group, split lays parts out as
    // [text, language, code, text, language, code, ...].
    for (let i = 0; i < parts.length; i++) {
      if (i % 3 === 1) continue; // the language label of a block (unused)
      const isCode = i % 3 === 2;
      if (isCode) {
        const pre = document.createElement("pre");
        pre.className = "ws-code";
        const code = document.createElement("code");
        code.textContent = parts[i].replace(/\n$/, "");
        const copy = document.createElement("button");
        copy.type = "button";
        copy.className = "ws-code-copy";
        copy.textContent = "Copy";
        copy.addEventListener("click", () => {
          navigator.clipboard.writeText(code.textContent).then(() => {
            copy.textContent = "Copied";
            setTimeout(() => (copy.textContent = "Copy"), 1500);
          });
        });
        pre.appendChild(copy);
        pre.appendChild(code);
        span.appendChild(pre);
      } else if (parts[i]) {
        span.appendChild(document.createTextNode(parts[i]));
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
    followupsRow.textContent = "";
    const stick = nearBottom();
    addRow("user", message);
    const row = addRow("orbit");
    const { body, reply } = orbitParts(row);
    setStatus("Thinking…");
    maybeScroll(true);

    let raw = "";
    let thinkingBlock = null;
    let failed = false;

    try {
      const resp = await fetch("/api/project/" + encodeURIComponent(projectId) + "/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        // agent: which conversation this turn belongs to (rail picks it);
        // task_id: the pinned "Now discussing" task, so the server can join
        // its real state to the prompt.
        body: JSON.stringify({ message, agent: agentId, task_id: pinned.taskId || undefined }),
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
              thinkingBlock = document.createElement("div");
              thinkingBlock.className = "thinking-block";
              const label = document.createElement("span");
              label.className = "thinking-label";
              label.textContent = "Narration";
              thinkingBlock.appendChild(label);
              body.insertBefore(thinkingBlock, reply);
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
            err.className = "ws-error";
            err.textContent = data || "Something went wrong on this turn.";
            body.appendChild(err);
          } else if (event === "done") {
            // finished below
          }
          maybeScroll(stickNow);
        }
      }
    } catch (err) {
      failed = true;
      const errLine = document.createElement("div");
      errLine.className = "ws-error";
      errLine.textContent = (err && err.message) || "The turn failed.";
      body.appendChild(errLine);
    } finally {
      if (raw) formatReply(reply, raw);
      if (!raw && !failed) {
        // A silent end looks exactly like a hang; say so instead.
        const errLine = document.createElement("div");
        errLine.className = "ws-error";
        errLine.textContent = "The provider returned an empty reply.";
        body.appendChild(errLine);
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
    const chip = e.target.closest(".ws-chip");
    if (chip) sendPrompt(chip.getAttribute("data-prompt"));
  });

  /* The task pin ("Task · …" above the composer): switching it redirects to
     the same workspace discussing that task, so the strip and prompt are
     server-rendered truth, never a client claim. */
  if (taskPin) {
    taskPin.addEventListener("change", () => {
      const id = taskPin.value;
      const url = "/projects/" + encodeURIComponent(projectId) +
        "?task=" + encodeURIComponent(id || "") +
        (agentId && agentId !== "main" ? "&agent=" + encodeURIComponent(agentId) : "") +
        "#agent";
      window.location.href = url;
    });
  }

  /* Chat / Agent and Quick / Deep: the workspace rail's compact forms of the
     same global settings the home topbar carries. Rendered by JS (the toggles
     fetch JSON), so without JavaScript nothing inert shows up. */
  function renderModeZone(currentMode, currentThinking) {
    if (!modeZone) return;
    modeZone.textContent = "";
    const seg = document.createElement("span");
    seg.className = "ws-mode-seg";
    ["chat", "agent"].forEach((m) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "ws-mode-opt" + (m === currentMode ? " active" : "");
      b.textContent = m === "chat" ? "Chat" : "Agent";
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
    think.className = "ws-mode-opt ws-think-opt";
    think.textContent = currentThinking === "deep" ? "Deep" : "Quick";
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

  /* "Plan with the Main Agent" drops you here ready to type. */
  if (composer.getAttribute("data-plan-hint") === "1" && !thread.children.length) {
    input.focus();
  }

  window.OrbitWorkspace = { sendPrompt };
  thread.scrollTop = thread.scrollHeight;
})();
