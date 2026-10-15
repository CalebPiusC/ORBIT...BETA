/* ===========================================================================
   ORBIT — onboarding client.

   Four jobs, in order of how much they matter:

   1. Never lose someone's work. The feedback drawer posts in place, the draft
      survives a close, and a rejected submit puts the text back in the box.
   2. Say what is happening. Loading buttons keep their size and swap their label;
      errors land beside the field and in a summary; nothing fails silently.
   3. Validate like an adult. Inline, on blur and on submit, with the first bad
      field focused — and always letting the server have the final word.
   4. Stay out of the way. No tour unless asked for, no hint twice, and every
      dismissal is remembered by the server, not just this browser.

   It is deliberately small and dependency-free. Anything the server already
   decides (validation rules, tour steps, context blocks) is passed in the markup
   rather than re-implemented here.
   =========================================================================== */

(function () {
  "use strict";

  var ORBIT = window.OrbitOnboarding || {};

  function $(selector, scope) {
    return (scope || document).querySelector(selector);
  }
  function $$(selector, scope) {
    return Array.prototype.slice.call((scope || document).querySelectorAll(selector));
  }

  function csrfToken() {
    var field = $('input[name="csrf_token"]');
    return field ? field.value : "";
  }

  function postJSON(url, payload) {
    return fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": csrfToken(),
        "X-Requested-With": "fetch"
      },
      body: JSON.stringify(payload || {}),
      credentials: "same-origin"
    }).then(function (response) {
      return response
        .json()
        .catch(function () {
          return { ok: false, error: "The server sent a response ORBIT could not read." };
        })
        .then(function (data) {
          return { status: response.status, data: data };
        });
    });
  }

  /* ---------------------------------------------------------------- toasts */

  function toast(message, tone) {
    var box = document.getElementById("obToast");
    var text = document.getElementById("obToastText");
    if (!box || !text) return;
    text.textContent = message;
    box.setAttribute("data-tone", tone || "success");
    box.hidden = false;
    var glyph = box.querySelector(".glyph");
    if (glyph) glyph.textContent = tone === "error" ? "!" : "✓";
    window.clearTimeout(toast._timer);
    toast._timer = window.setTimeout(function () {
      box.hidden = true;
    }, tone === "error" ? 7000 : 4200);
  }
  ORBIT.toast = toast;

  /* ------------------------------------------------------------ flash decay */

  function initFlashes() {
    $$("[data-dismiss-flash]").forEach(function (button) {
      button.addEventListener("click", function () {
        var alert = button.closest(".ob-alert");
        if (alert) alert.remove();
      });
    });
  }

  /* ------------------------------------------------ loading button behavior */

  function initLoadingForms() {
    $$("form").forEach(function (form) {
      form.addEventListener("submit", function (event) {
        var submitter = event.submitter || $(('button[type="submit"], button:not([type])'), form);
        if (!submitter || submitter.disabled) return;
        // A form that failed client-side validation never gets here.
        if (form.dataset.invalid === "true") return;
        var label = submitter.querySelector(".ob-btn-label");
        if (label && submitter.dataset.loadingLabel) {
          submitter.dataset.originalLabel = label.textContent;
          label.textContent = submitter.dataset.loadingLabel;
        }
        submitter.setAttribute("data-loading", "true");
        submitter.setAttribute("aria-busy", "true");
        form.setAttribute("aria-busy", "true");
        // Nothing is disabled: a submit that never returns should still be
        // escapable, and disabling the button loses the ability to press it twice
        // to be sure the click registered.
      });
    });
  }

  /* --------------------------------------------------------- form validation */

  var EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

  function fieldError(input, message) {
    var holder = input.closest(".ob-field") || input.parentNode;
    var existing = holder.querySelector(".ob-error[data-generated]");
    if (existing) existing.remove();
    if (!message) {
      input.removeAttribute("aria-invalid");
      return;
    }
    input.setAttribute("aria-invalid", "true");
    var span = document.createElement("span");
    span.className = "ob-error";
    span.setAttribute("data-generated", "true");
    span.setAttribute("role", "alert");
    span.textContent = message;
    holder.appendChild(span);
  }

  function validateInput(input) {
    if (input.disabled || input.type === "hidden" || input.type === "checkbox" || input.type === "radio") {
      return true;
    }
    var value = (input.value || "").trim();
    var label = input.closest(".ob-field");
    var name = input.name || "";
    if (input.required && !value) {
      fieldError(input, "This one is needed.");
      return false;
    }
    if (name === "email" && value && !EMAIL_RE.test(value)) {
      fieldError(input, "That email address looks incomplete.");
      return false;
    }
    if (input.type === "password" && input.dataset.passwordMeter && value) {
      var problems = passwordProblems(input, value);
      if (problems.length) {
        fieldError(input, problems[0]);
        return false;
      }
    }
    if (name === "confirm" && value) {
      var other = input.form ? input.form.querySelector('input[name="password"]') : null;
      if (other && other.value && other.value !== value) {
        fieldError(input, "Those two passwords don't match.");
        return false;
      }
    }
    fieldError(input, "");
    if (label) label.classList.remove("ob-choice--bad");
    return true;
  }

  function passwordProblems(input, value) {
    var problems = [];
    var email = input.form ? input.form.querySelector('input[name="email"]') : null;
    var nameInput = input.form ? input.form.querySelector('input[name="name"]') : null;
    if (value.length < 10) problems.push("Use at least 10 characters.");
    if (weakPasswords.indexOf(value.toLowerCase()) !== -1) {
      problems.push("That password is one of the first ones attackers try.");
    }
    if (email && email.value) {
      var local = (email.value.split("@")[0] || "").toLowerCase();
      if (local.length > 2 && value.toLowerCase().indexOf(local) !== -1) {
        problems.push("Don't build it out of your email address.");
      }
    }
    if (nameInput && nameInput.value) {
      var first = (nameInput.value.split(" ")[0] || "").toLowerCase();
      if (first.length > 2 && value.toLowerCase().indexOf(first) !== -1) {
        problems.push("Don't build it out of your own name.");
      }
    }
    return problems;
  }

  var weakPasswords = [
    "password", "password1", "password123", "1234567890", "qwertyuiop",
    "letmein123", "iloveyou1", "admin12345", "welcome123", "changeme123",
    "orbitorbit", "passw0rd123", "1q2w3e4r5t", "abc12345678"
  ];

  function initValidation() {
    $$("form[data-validate-form]").forEach(function (form) {
      var inputs = $$("input, textarea, select", form).filter(function (input) {
        return input.type !== "hidden" && input.type !== "submit" && input.type !== "checkbox" && input.type !== "radio";
      });
      inputs.forEach(function (input) {
        input.addEventListener("blur", function () {
          if (!input.value && !input.required) return;
          validateInput(input);
        });
        input.addEventListener("input", function () {
          if (input.getAttribute("aria-invalid") === "true") validateInput(input);
        });
      });

      form.addEventListener("submit", function (event) {
        var firstBad = null;
        var ok = true;
        inputs.forEach(function (input) {
          if (!validateInput(input)) {
            ok = false;
            if (!firstBad) firstBad = input;
          }
          // The password rules are off by default in this builder (a rule that
          // blocks a submit would be a policy change, not a hint), so only the
          // length and personal-detail rules above can stop it.
          if (input.dataset.passwordMeter) {
            var problems = passwordProblems(input, input.value);
            if (problems.length) {
              ok = false;
              if (!firstBad) firstBad = input;
              fieldError(input, problems[0]);
            } else if (input.value) {
              fieldError(input, "");
            }
          }
        });
        var terms = form.querySelector('input[name="terms"][type="checkbox"]');
        if (terms && !terms.checked) {
          ok = false;
          var check = terms.closest(".ob-check");
          if (check) {
            check.classList.add("ob-check--bad");
            if (!check.querySelector("[data-generated]")) {
              var span = document.createElement("span");
              span.className = "ob-error";
              span.setAttribute("data-generated", "true");
              span.textContent = "Please accept the beta terms to continue.";
              check.parentNode.appendChild(span);
            }
          }
          if (!firstBad) firstBad = terms;
        }
        form.dataset.invalid = ok ? "false" : "true";
        if (!ok) {
          event.preventDefault();
          var summary = form.querySelector("[data-error-summary]");
          if (summary) summary.focus();
          if (firstBad) {
            firstBad.focus();
            if (firstBad.scrollIntoView) firstBad.scrollIntoView({ block: "center", behavior: "smooth" });
          }
          return;
        }
        // Let it through; the loading state takes over from here.
        form.dataset.invalid = "false";
      });
    });
  }

  /* ------------------------------------------------------- password strength */

  function initPasswordMeters() {
    $$("[data-password-meter]").forEach(function (input) {
      var meter = document.querySelector('[data-meter-for="' + input.dataset.passwordMeter + '"]');
      var label = document.getElementById(input.id + "-strength");
      var rules = document.getElementById(input.id + "-rules");
      function refresh() {
        var value = input.value || "";
        var score = 0;
        if (value.length >= 8) score += 1;
        if (value.length >= 10) score += 1;
        if (value.length >= 16) score += 1;
        var classes = 0;
        if (/[a-z]/.test(value)) classes += 1;
        if (/[A-Z]/.test(value)) classes += 1;
        if (/[0-9]/.test(value)) classes += 1;
        if (/[^A-Za-z0-9]/.test(value)) classes += 1;
        score += Math.max(0, classes - 1);
        if (weakPasswords.indexOf(value.toLowerCase()) !== -1) score = 0;
        score = Math.max(0, Math.min(4, score));
        if (meter) meter.setAttribute("data-score", value ? String(score) : "0");
        if (label) {
          label.textContent = value
            ? "Strength: " + ["very weak", "weak", "fair", "good", "strong"][score]
            : "Strength: not yet rated";
        }
        if (rules) {
          var problems = value ? passwordProblems(input, value) : ["", "", ""];
          var checks = {
            length: value.length >= 10,
            common: value.length > 0 && problems.indexOf("That password is one of the first ones attackers try.") === -1,
            personal:
              value.length > 0 &&
              problems.indexOf("Don't build it out of your email address.") === -1 &&
              problems.indexOf("Don't build it out of your own name.") === -1
          };
          $$("li", rules).forEach(function (item) {
            var key = item.getAttribute("data-rule");
            item.setAttribute("data-ok", checks[key] ? "true" : "false");
          });
        }
      }
      input.addEventListener("input", refresh);
      refresh();
    });
  }

  /* ------------------------------------------------- tiny interaction helpers */

  function initDisclosures() {
    $$("[data-toggle-panel]").forEach(function (button) {
      var panel = document.getElementById(button.getAttribute("data-toggle-panel"));
      if (!panel) return;
      button.addEventListener("click", function () {
        var open = panel.hidden;
        panel.hidden = !open;
        button.setAttribute("aria-expanded", open ? "true" : "false");
        if (open) {
          var first = panel.querySelector("input, textarea, button");
          if (first) first.focus();
        }
      });
    });
  }

  function initCountdowns() {
    $$("[data-resend-countdown]").forEach(function (span) {
      var button = span.closest(".ob-link-row") ? span.closest(".ob-link-row").querySelector("[data-enable-after]") : null;
      var remaining = parseInt(span.getAttribute("data-seconds"), 10) || 0;
      if (remaining <= 0) return;
      if (button) button.disabled = true;
      var timer = window.setInterval(function () {
        remaining -= 1;
        span.textContent = remaining + "s";
        if (remaining <= 0) {
          window.clearInterval(timer);
          span.textContent = "now";
          if (button) button.disabled = false;
        }
      }, 1000);
    });
  }

  function initCustomChips() {
    $$("[data-custom-add]").forEach(function (button) {
      var key = button.getAttribute("data-custom-add");
      var input = document.querySelector('[data-custom-input="' + key + '"]');
      var list = document.querySelector('[data-custom-list="' + key + '"]');
      if (!input || !list) return;
      function add() {
        var value = (input.value || "").trim();
        if (!value) return;
        var already = $$("input", list).some(function (hidden) {
          return hidden.value.toLowerCase() === value.toLowerCase();
        });
        if (already) {
          ORBIT.toast("Already in the list.", "error");
          input.value = "";
          return;
        }
        var tag = document.createElement("span");
        tag.className = "tag";
        tag.textContent = value + " ";
        var hidden = document.createElement("input");
        hidden.type = "hidden";
        hidden.name = key;
        hidden.value = value;
        var remove = document.createElement("button");
        remove.type = "button";
        remove.setAttribute("data-custom-remove", "");
        remove.setAttribute("aria-label", "Remove " + value);
        remove.textContent = "✕";
        tag.appendChild(hidden);
        tag.appendChild(remove);
        list.appendChild(tag);
        input.value = "";
      }
      button.addEventListener("click", add);
      input.addEventListener("keydown", function (event) {
        if (event.key === "Enter") {
          event.preventDefault();
          add();
        }
      });
    });
    document.addEventListener("click", function (event) {
      var remove = event.target.closest("[data-custom-remove]");
      if (remove) {
        var tag = remove.closest(".tag");
        if (tag) tag.remove();
      }
    });
  }

  function initFilePickers() {
    $$("[data-file-input], [data-drop] input[type=file], input[name=archive]").forEach(function (input) {
      input.addEventListener("change", function () {
        var holder = input.closest(".ob-drop") || input.parentNode;
        var label = holder ? holder.querySelector("[data-file-name]") : null;
        if (!label) return;
        var names = Array.prototype.slice.call(input.files || []).map(function (file) {
          return file.name;
        });
        label.textContent = names.length
          ? names.length + " selected: " + names.slice(0, 3).join(", ") + (names.length > 3 ? "…" : "")
          : "";
      });
    });

    var folderInput = document.querySelector("[data-folder-input]");
    if (folderInput) {
      folderInput.addEventListener("change", function () {
        var files = Array.prototype.slice.call(folderInput.files || []);
        var container = document.querySelector("[data-folder-paths]");
        var counter = document.querySelector("[data-folder-count]");
        if (counter) {
          counter.textContent = files.length ? files.length + " files will be listed" : "";
        }
        if (!container) return;
        container.innerHTML = "";
        // webkitRelativePath is the browser's relative path list. These inputs are
        // names only: the server keeps a listing, and the files never leave the
        // machine. The copy on the page says exactly that.
        files.slice(0, 400).forEach(function (file) {
          var input = document.createElement("input");
          input.type = "hidden";
          input.name = "paths";
          input.value = file.webkitRelativePath || file.name;
          container.appendChild(input);
        });
      });
    }
  }

  /* --------------------------------------------------------- onboarding APIs */

  function markOnboarding(changes) {
    return postJSON("/api/onboarding/tour", changes).then(function (result) {
      if (!result.data || !result.data.ok) {
        toast((result.data && result.data.error) || "That could not be saved.", "error");
      }
      return result;
    });
  }

  function initHints() {
    $$("[data-dismiss-hint]").forEach(function (button) {
      button.addEventListener("click", function () {
        var hint = button.closest(".ob-hint");
        var id = button.getAttribute("data-dismiss-hint");
        if (hint) hint.remove();
        fetch("/api/onboarding/hints/" + encodeURIComponent(id) + "/dismiss", {
          method: "POST",
          headers: { "X-CSRF-Token": csrfToken() },
          credentials: "same-origin"
        }).catch(function () {
          /* the hint stays hidden for this view; it will reappear next visit */
        });
      });
    });

    var checklistClose = $("[data-dismiss-checklist]");
    if (checklistClose) {
      checklistClose.addEventListener("click", function () {
        var block = document.getElementById("gettingStarted");
        if (block) block.remove();
        postJSON("/api/onboarding/getting-started", { dismiss: true }).catch(function () {});
      });
    }
  }

  /* ------------------------------------------------------------- the tour */

  function initTour() {
    var tour = document.getElementById("obTour");
    if (!tour) return;
    var steps = [];
    try {
      steps = JSON.parse(tour.getAttribute("data-steps") || "[]");
    } catch (error) {
      steps = [];
    }
    if (!steps.length) return;

    var spot = document.getElementById("obTourSpot");
    var title = document.getElementById("obTourTitle");
    var body = document.getElementById("obTourBody");
    var count = document.getElementById("obTourCount");
    var progress = document.getElementById("obTourProgress");
    var next = $("[data-tour-next]", tour);
    var prev = $("[data-tour-prev]", tour);
    var card = document.getElementById("obTourCard");
    var index = 0;
    var lastFocused = null;

    // Steps whose anchor is not on this screen are dropped rather than pointing at
    // nothing. On the settings screen, for instance, only the sidebar stop applies.
    steps = steps.filter(function (step) {
      step.element = document.querySelector(step.selector);
      return !!step.element;
    });
    if (!steps.length) return;

    progress.innerHTML = steps
      .map(function () {
        return "<i></i>";
      })
      .join("");

    function paint() {
      var step = steps[index];
      title.textContent = step.title;
      body.textContent = step.body;
      count.textContent = "Step " + (index + 1) + " of " + steps.length;
      $$("i", progress).forEach(function (bar, position) {
        bar.setAttribute("data-on", position <= index ? "true" : "false");
      });
      if (prev) prev.hidden = index === 0;
      if (next) next.textContent = index === steps.length - 1 ? "Finish" : "Next";
      if (step.element && step.element.scrollIntoView) {
        step.element.scrollIntoView({ block: "center", behavior: "smooth" });
      }
      window.requestAnimationFrame(positionSpotlight);
    }

    function positionSpotlight() {
      var step = steps[index];
      if (!spot || !step.element) return;
      var rect = step.element.getBoundingClientRect();
      var pad = 6;
      spot.style.top = Math.max(0, rect.top - pad) + "px";
      spot.style.left = Math.max(0, rect.left - pad) + "px";
      spot.style.width = rect.width + pad * 2 + "px";
      spot.style.height = rect.height + pad * 2 + "px";
    }

    function open() {
      lastFocused = document.activeElement;
      tour.hidden = false;
      index = 0;
      paint();
      if (next) next.focus();
      window.addEventListener("resize", positionSpotlight);
      window.addEventListener("scroll", positionSpotlight, true);
    }

    function close(seen) {
      tour.hidden = true;
      window.removeEventListener("resize", positionSpotlight);
      window.removeEventListener("scroll", positionSpotlight, true);
      if (seen) markOnboarding({ seen: true });
      if (lastFocused && lastFocused.focus) lastFocused.focus();
    }

    if (next) {
      next.addEventListener("click", function () {
        if (index === steps.length - 1) {
          close(true);
          toast("Tour finished. You can replay it from Settings → Help & beta.");
          return;
        }
        index += 1;
        paint();
      });
    }
    if (prev) {
      prev.addEventListener("click", function () {
        index = Math.max(0, index - 1);
        paint();
      });
    }
    $$("[data-tour-skip]", tour).forEach(function (button) {
      button.addEventListener("click", function () {
        close(true);
      });
    });
    $$("[data-tour-dismiss]", tour).forEach(function (button) {
      button.addEventListener("click", function () {
        markOnboarding({ seen: true, opt_out: true }).then(function () {
          toast("Understood — no more tour offers. It is still in Settings → Help.");
        });
        tour.hidden = true;
      });
    });
    $$("[data-tour-start]").forEach(function (button) {
      button.addEventListener("click", open);
    });

    document.addEventListener("keydown", function (event) {
      if (tour.hidden) return;
      if (event.key === "Escape") {
        close(true);
      } else if (event.key === "ArrowRight" && next) {
        next.click();
      } else if (event.key === "ArrowLeft" && prev && !prev.hidden) {
        prev.click();
      } else if (event.key === "Tab") {
        // A real focus trap: the backdrop is not interactive, so the tour's own
        // controls are the only tab stops while it is open.
        var focusables = $$("button:not([hidden])", card);
        if (!focusables.length) return;
        var first = focusables[0];
        var last = focusables[focusables.length - 1];
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      }
    });

    if (tour.getAttribute("data-start") === "1" || location.search.indexOf("tour=1") !== -1) {
      window.setTimeout(open, 350);
    }
  }

  /* -------------------------------------------------------- feedback drawer */

  var DRAFT_KEY = "orbit.feedback.draft";

  function initFeedback() {
    var fab = document.getElementById("obFeedbackFab");
    var drawer = document.getElementById("obFeedbackDrawer");
    var backdrop = document.getElementById("obDrawerBackdrop");
    var form = document.getElementById("obFeedbackForm");
    if (!fab || !drawer || !form) return;

    var bodyField = form.querySelector('textarea[name="body"]');
    var subjectField = form.querySelector('input[name="subject"]');
    var status = document.getElementById("obFeedbackStatus");
    var submit = document.querySelector("[data-feedback-submit]");

    function rememberDraft() {
      try {
        window.sessionStorage.setItem(
          DRAFT_KEY,
          JSON.stringify({
            body: bodyField ? bodyField.value : "",
            subject: subjectField ? subjectField.value : "",
            kind: (form.querySelector('input[name="kind"]:checked') || {}).value || "suggestion"
          })
        );
      } catch (error) {
        /* private mode: the form still works, it just does not remember */
      }
    }

    function restoreDraft() {
      try {
        var raw = window.sessionStorage.getItem(DRAFT_KEY);
        if (!raw) return;
        var draft = JSON.parse(raw);
        if (bodyField && draft.body) bodyField.value = draft.body;
        if (subjectField && draft.subject) subjectField.value = draft.subject;
        if (draft.kind) {
          var radio = form.querySelector('input[name="kind"][value="' + draft.kind + '"]');
          if (radio) radio.checked = true;
        }
      } catch (error) {
        /* ignore a malformed draft */
      }
    }

    function clearDraft() {
      try {
        window.sessionStorage.removeItem(DRAFT_KEY);
      } catch (error) {
        /* ignore */
      }
    }

    function open() {
      restoreDraft();
      // The server cannot know the window size, so the person's own viewport is
      // added to the visible context block the moment it becomes relevant.
      var viewport = document.querySelector("[data-context-viewport]");
      if (!viewport) {
        var list = drawer.querySelector(".ob-context ul");
        if (list) {
          var item = document.createElement("li");
          item.setAttribute("data-context-viewport", "true");
          var key = document.createElement("b");
          key.textContent = "Window";
          var value = document.createElement("span");
          value.textContent = window.innerWidth + " × " + window.innerHeight + " px";
          item.appendChild(key);
          item.appendChild(value);
          list.appendChild(item);
        }
      }
      drawer.hidden = false;
      backdrop.hidden = false;
      window.requestAnimationFrame(function () {
        drawer.setAttribute("data-open", "true");
      });
      fab.setAttribute("aria-expanded", "true");
      if (bodyField) bodyField.focus();
      rememberDraft();
    }

    function close() {
      drawer.setAttribute("data-open", "false");
      fab.setAttribute("aria-expanded", "false");
      window.setTimeout(function () {
        drawer.hidden = true;
        backdrop.hidden = true;
      }, 200);
      rememberDraft();
      fab.focus();
    }

    fab.addEventListener("click", open);
    backdrop.addEventListener("click", close);
    $$("[data-close-feedback]", drawer).forEach(function (button) {
      button.addEventListener("click", close);
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && !drawer.hidden) close();
    });
    if (bodyField) bodyField.addEventListener("input", rememberDraft);

    // The context block can be dropped entirely before sending.
    var contextToggle = drawer.querySelector("[data-context-toggle]");
    if (contextToggle) {
      contextToggle.addEventListener("change", function () {
        var list = drawer.querySelector(".ob-context ul");
        if (!list) return;
        list.style.display = contextToggle.checked ? "" : "none";
      });
    }

    form.addEventListener("submit", function (event) {
      event.preventDefault();
      var body = bodyField ? bodyField.value.trim() : "";
      var errorBox = document.getElementById("obFeedbackBodyError");
      if (body.length < 12) {
        if (errorBox) {
          errorBox.textContent = "A little more detail, please — twelve characters or so.";
          errorBox.hidden = false;
        }
        if (bodyField) {
          bodyField.setAttribute("aria-invalid", "true");
          bodyField.focus();
        }
        return;
      }
      if (errorBox) errorBox.hidden = true;
      if (bodyField) bodyField.removeAttribute("aria-invalid");

      var included = !contextToggle || contextToggle.checked;
      var context = {};
      if (included) {
        $$("li", drawer.querySelector(".ob-context")).forEach(function (item) {
          var key = item.querySelector("b");
          var value = item.querySelector("span");
          if (key && value) context[key.textContent.toLowerCase()] = value.textContent;
        });
        context.route = window.location.pathname;
        var viewportItem = drawer.querySelector("[data-context-viewport] span");
        if (viewportItem) context.window = viewportItem.textContent;
      }

      var originalLabel = submit ? submit.querySelector(".ob-btn-label").textContent : "";
      if (submit) {
        submit.setAttribute("data-loading", "true");
        submit.querySelector(".ob-btn-label").textContent = "Sending…";
      }
      if (status) status.textContent = "";

      postJSON("/api/onboarding/feedback", {
        kind: (form.querySelector('input[name="kind"]:checked') || {}).value || "general",
        body: body,
        subject: subjectField ? subjectField.value : "",
        context: context
      })
        .then(function (result) {
          if (submit) {
            submit.removeAttribute("data-loading");
            submit.querySelector(".ob-btn-label").textContent = originalLabel;
          }
          if (!result.data || !result.data.ok) {
            var problems = (result.data && result.data.problems) || [];
            if (status) {
              status.textContent = problems.length
                ? problems.join(" ")
                : (result.data && result.data.error) || "That could not be sent. Your text is still here.";
            }
            toast("Not sent — your text is still in the box.", "error");
            return;
          }
          clearDraft();
          if (bodyField) bodyField.value = "";
          if (subjectField) subjectField.value = "";
          if (status) {
            status.textContent = "Sent · reference " + result.data.reference;
          }
          toast("Feedback sent — reference " + result.data.reference + ". Nothing in your workspace changed.");
          window.setTimeout(close, 900);
        })
        .catch(function () {
          if (submit) {
            submit.removeAttribute("data-loading");
            submit.querySelector(".ob-btn-label").textContent = originalLabel;
          }
          if (status) status.textContent = "The request never reached the server. Your text is still here.";
          toast("Connection problem — your text is still in the box.", "error");
        });
    });
  }

  /* ------------------------------------------------------------ page forms */

  function initFeedbackPageDrafts() {
    var form = document.querySelector('form[action$="/feedback"] textarea[name="body"]');
    if (!form) return;
    var key = "orbit.feedback.draft.page";
    try {
      var saved = window.sessionStorage.getItem(key);
      if (saved && !form.value) form.value = saved;
    } catch (error) {
      /* ignore */
    }
    form.addEventListener("input", function () {
      try {
        window.sessionStorage.setItem(key, form.value);
      } catch (error) {
        /* ignore */
      }
    });
    form.closest("form").addEventListener("submit", function () {
      try {
        window.sessionStorage.removeItem(key);
      } catch (error) {
        /* ignore */
      }
    });
  }

  /* ------------------------------------------------------------------ boot */

  function init() {
    initFlashes();
    initLoadingForms();
    initValidation();
    initPasswordMeters();
    initDisclosures();
    initCountdowns();
    initCustomChips();
    initFilePickers();
    initHints();
    initTour();
    initFeedback();
    initFeedbackPageDrafts();
    var autofocus = document.querySelector("[data-autofocus]");
    if (autofocus && !document.querySelector(".ob-alert--error")) autofocus.focus();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  window.OrbitOnboarding = ORBIT;
  window.OrbitOnboarding.postJSON = postJSON;
  window.OrbitOnboarding.markOnboarding = markOnboarding;
})();
