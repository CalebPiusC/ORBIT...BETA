/* Signed-in onboarding behaviors (layout.html loads this only when the launch
 * has an account store):
 *
 *   * the floating beta-feedback dialog — opens from the Feedback button on any
 *     page, submits over fetch, and never navigates away, so a report never
 *     costs the work on screen;
 *   * the new-user checklist's dismiss control;
 *   * the first-run tour: a short spotlight walk over elements that really
 *     exist on the dashboard. Skippable at every step; finishing or skipping
 *     is recorded server-side so it never nags twice. Settings can replay it;
 *   * the settings page's type-to-confirm account deletion;
 *   * busy states on app-side forms that opt in (data-busy-label).
 */
(function () {
  "use strict";

  function postJSON(url, payload) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify(payload || {}),
    }).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (data) {
        return { ok: response.ok, status: response.status, data: data };
      });
    });
  }

  function busyOn(button) {
    if (!button || button.classList.contains("is-busy")) return;
    var label = button.querySelector(".btn-label");
    var busyLabel = button.getAttribute("data-busy-label");
    if (label && busyLabel) {
      label.setAttribute("data-idle-label", label.textContent);
      label.textContent = busyLabel;
    }
    button.classList.add("is-busy");
    button.disabled = true;
  }

  function busyOff(button) {
    if (!button) return;
    var label = button.querySelector(".btn-label");
    var idle = label && label.getAttribute("data-idle-label");
    if (label && idle) label.textContent = idle;
    button.classList.remove("is-busy");
    button.disabled = false;
  }

  /* ================= the floating feedback dialog ================= */

  function wireFeedbackWidget() {
    var fab = document.getElementById("feedbackFab");
    var dialog = document.getElementById("feedbackDialog");
    if (!fab || !dialog) return;
    var form = dialog.querySelector("[data-feedback-widget-form]");
    var send = dialog.querySelector("[data-feedback-send]");
    var errorBox = dialog.querySelector("[data-feedback-error]");
    var pageLabel = dialog.querySelector("[data-feedback-page-label]");
    var toast = document.getElementById("feedbackToast");
    var toastTimer = null;

    function currentPath() {
      try { return window.location.pathname + window.location.hash; } catch (_e) { return ""; }
    }

    fab.addEventListener("click", function () {
      if (pageLabel) pageLabel.textContent = currentPath();
      if (errorBox) { errorBox.hidden = true; errorBox.textContent = ""; }
      // <dialog>.showModal is the modal path; older engines still get a usable
      // sheet via the `open` attribute instead of a dead button.
      if (typeof dialog.showModal === "function") dialog.showModal();
      else { dialog.setAttribute("open", ""); dialog.style.zIndex = "2401"; }
    });

    function showToast() {
      if (!toast) return;
      toast.hidden = false;
      if (toastTimer) clearTimeout(toastTimer);
      toastTimer = setTimeout(function () { toast.hidden = true; }, 4000);
    }

    if (send) send.addEventListener("click", function () {
      var kind = form.querySelector('input[name="kind"]:checked');
      var title = form.querySelector('[name="title"]');
      var body = form.querySelector('[name="body"]');
      var includePage = form.querySelector('[name="include_page"]');
      var error = "";
      if (!title.value.trim()) error = "Give it a short title — one sentence is fine.";
      else if (!body.value.trim()) error = "A line or two of detail helps us actually fix it.";
      if (error) {
        errorBox.textContent = error;
        errorBox.hidden = false;
        return;
      }
      errorBox.hidden = true;
      busyOn(send);
      postJSON("/api/feedback", {
        kind: kind ? kind.value : "general",
        title: title.value.trim(),
        body: body.value.trim(),
        page: includePage && includePage.checked ? currentPath() : "",
      }).then(function (result) {
        busyOff(send);
        if (!result.ok) {
          errorBox.textContent = (result.data && result.data.error) || "Couldn't send — try again in a moment.";
          errorBox.hidden = false;
          return;
        }
        // Success: close quietly, confirm with a toast, and leave the page
        // exactly as it was. That is the whole point of the floating dialog.
        title.value = "";
        body.value = "";
        if (typeof dialog.close === "function") dialog.close();
        else { dialog.removeAttribute("open"); dialog.style.zIndex = ""; }
        showToast();
      }).catch(function () {
        busyOff(send);
        errorBox.textContent = "Couldn't reach the server — your text is still here, try again.";
        errorBox.hidden = false;
      });
    });
  }

  /* ================= the dashboard checklist ================= */

  function wireChecklist() {
    var button = document.querySelector("[data-checklist-dismiss]");
    if (!button) return;
    button.addEventListener("click", function () {
      var card = button.closest(".checklist-card");
      if (card) card.style.display = "none";
      postJSON("/api/onboarding/checklist", { dismissed: true }).catch(function () {
        // The hide already happened; the server catch-up can fail silently
        // without resurrecting the card this session.
      });
    });
  }

  /* ================= the first-run tour =================
     Steps name only things that exist in the product, and each step declares
     the selectors it can anchor to (first match wins). A step whose targets
     are all hidden or absent is skipped — the tour describes, it never
     bluffs. */

  var TOUR_STEPS = [
    {
      targets: [],
      title: "Welcome to your workspace",
      text: "This is a sixty-second tour of what ORBIT actually does — four quick stops. Skipping is one click, and it stays skipped.",
    },
    {
      targets: ['[data-tour="start-paths"]'],
      title: "Three real ways to start",
      text: "Every path creates a genuine project and puts you in its workspace — create directly, walk the guided setup, or describe the idea to the Main Agent and plan it together. Nothing here is a dummy form.",
    },
    {
      targets: ['#sidebar .sidebar-nav', ".mobile-topbar .mobile-nav"],
      title: "Your work lives over here",
      text: "Projects have their own map of tasks and a Main Agent that knows the goal. The Agents page holds six specialists — Coding, Review, Debug and friends — you can assign to any project.",
    },
    {
      targets: ['[data-tour="checklist"]', '[data-tour="resume"]', '[data-tour="greeting"]'],
      title: "It picks up where you stop",
      text: "The checklist closes itself as you go (and its ✕ hides it for good). Come back with projects in flight and a “jump back in” strip lands you exactly where you left off.",
    },
    {
      targets: ['#feedbackFab'],
      title: "The beta runs on this button",
      text: "It's on every page, it never navigates away, and everything you send is listed on the Feedback page. Rough edges and missing features both go straight into what ships next.",
    },
    {
      targets: [],
      title: "You're set.",
      text: "That's the tour — the rest you'll learn by building. Settings → Account replays it any time. Now go put something in orbit.",
    },
  ];

  function startTour() {
    if (window.__orbitTourRunning) return;
    window.__orbitTourRunning = true;

    var highlight = document.createElement("div");
    highlight.className = "tour-highlight";
    highlight.hidden = true;
    var card = document.createElement("div");
    card.className = "tour-card";
    card.setAttribute("role", "dialog");
    card.setAttribute("aria-modal", "false");
    card.setAttribute("aria-label", "Product tour");
    card.hidden = true;
    document.body.appendChild(highlight);
    document.body.appendChild(card);

    var index = 0;
    var finished = false;

    function targetRect(selectors) {
      for (var i = 0; i < selectors.length; i += 1) {
        var el = document.querySelector(selectors[i]);
        if (!el) continue;
        var rect = el.getBoundingClientRect();
        if (rect.width > 0 && rect.height > 0) return { rect: rect, el: el };
      }
      return null;
    }

    function endTour(state) {
      if (finished) return;
      finished = true;
      highlight.hidden = true;
      card.hidden = true;
      document.removeEventListener("keydown", onKey, true);
      window.removeEventListener("resize", layout);
      window.removeEventListener("scroll", layout, true);
      if (highlight.parentNode) highlight.parentNode.removeChild(highlight);
      if (card.parentNode) card.parentNode.removeChild(card);
      window.__orbitTourRunning = false;
      postJSON("/api/onboarding/tour", { state: state }).catch(function () {
        // Recording is best-effort; the tour never holds the interface hostage.
      });
    }

    function layout() {
      var step = TOUR_STEPS[index];
      var found = targetRect(step.targets || []);
      var margin = 10;
      var cardWidth = Math.min(340, window.innerWidth - 32);
      card.style.width = cardWidth + "px";
      if (!found) {
        highlight.hidden = true;
        // Centered step (intro and outro).
        card.style.left = Math.max(16, (window.innerWidth - cardWidth) / 2) + "px";
        card.style.top = Math.max(48, window.innerHeight * 0.24) + "px";
        card.hidden = false;
        return;
      }
      var rect = found.rect;
      highlight.hidden = false;
      highlight.style.left = (rect.left - margin / 2) + "px";
      highlight.style.top = (rect.top - margin / 2) + "px";
      highlight.style.width = (rect.width + margin) + "px";
      highlight.style.height = (rect.height + margin) + "px";
      // Prefer below the target; flip above when there is no room.
      var below = rect.bottom + 14;
      var cardHeight = card.offsetHeight || 180;
      var top = below;
      if (below + cardHeight > window.innerHeight - 16) {
        top = Math.max(16, rect.top - cardHeight - 14);
      }
      var left = Math.min(Math.max(16, rect.left), window.innerWidth - cardWidth - 16);
      card.style.left = left + "px";
      card.style.top = top + "px";
      card.hidden = false;
    }

    function render() {
      var step = TOUR_STEPS[index];
      card.innerHTML = "";
      var count = document.createElement("span");
      count.className = "tour-step-count";
      count.textContent = "Tour · " + (index + 1) + " of " + TOUR_STEPS.length;
      var title = document.createElement("h3");
      title.className = "tour-title";
      title.textContent = step.title;
      var text = document.createElement("p");
      text.className = "tour-text";
      text.textContent = step.text;
      var actions = document.createElement("div");
      actions.className = "tour-actions";
      var skip = document.createElement("button");
      skip.type = "button";
      skip.className = "tour-skip";
      skip.textContent = "Skip the tour";
      skip.addEventListener("click", function () { endTour("skipped"); });
      var spacer = document.createElement("span");
      spacer.className = "spacer";
      var next = document.createElement("button");
      next.type = "button";
      next.className = "tour-next";
      next.textContent = index === TOUR_STEPS.length - 1 ? "Done" : "Next";
      next.addEventListener("click", function () {
        if (index === TOUR_STEPS.length - 1) { endTour("done"); return; }
        // Advance past steps whose targets are not on this page.
        do { index += 1; } while (
          index < TOUR_STEPS.length - 1 &&
          TOUR_STEPS[index].targets.length && !targetRect(TOUR_STEPS[index].targets)
        );
        render();
      });
      actions.appendChild(skip);
      actions.appendChild(spacer);
      actions.appendChild(next);
      card.appendChild(count);
      card.appendChild(title);
      card.appendChild(text);
      card.appendChild(actions);
      layout();
      next.focus({ preventScroll: true });
    }

    function onKey(event) {
      if (event.key === "Escape") {
        event.stopPropagation();
        endTour("skipped");
      }
    }

    document.addEventListener("keydown", onKey, true);
    window.addEventListener("resize", layout);
    window.addEventListener("scroll", layout, true);
    // Skip straight past steps that have no targets on this page state.
    while (index < TOUR_STEPS.length - 1 && TOUR_STEPS[index].targets.length && !targetRect(TOUR_STEPS[index].targets)) {
      index += 1;
    }
    render();
  }

  function maybeTour() {
    var screen = document.querySelector("[data-tour-autostart]");
    if (!screen) return;
    // Let the page settle (fonts, sidebar collapse) before measuring rects.
    setTimeout(startTour, 420);
  }

  /* ================= settings: type-to-confirm deletion ================= */

  function wireConfirmForm() {
    var form = document.querySelector("[data-confirm-form]");
    if (!form) return;
    var input = form.querySelector("[data-confirm-match]");
    var button = form.querySelector("[data-confirm-button]");
    if (!input || !button) return;
    var expected = (input.getAttribute("data-confirm-match") || "").trim().toLowerCase();
    input.addEventListener("input", function () {
      button.disabled = (input.value || "").trim().toLowerCase() !== expected;
    });
  }

  /* ================= app-side busy states for opting-in forms ================= */

  function wireBusyForms() {
    document.querySelectorAll("form [data-busy-label]").forEach(function (button) {
      var form = button.closest("form");
      if (!form || form.__orbitBusyWired) return;
      form.__orbitBusyWired = true;
      form.addEventListener("submit", function () { busyOn(button); });
    });
  }

  function boot() {
    wireFeedbackWidget();
    wireChecklist();
    wireConfirmForm();
    wireBusyForms();
    maybeTour();
  }

  window.OrbitOnboarding = { boot: boot, startTour: startTour };
})();
