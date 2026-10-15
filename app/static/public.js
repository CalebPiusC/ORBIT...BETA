/* Public-page behaviors for the onboarding shell (public.html):
 *
 *   * inline form validation that never replaces the server's, only softens it —
 *     server errors render as the .auth-error box on the re-rendered page;
 *   * the password strength meter (mirrors the same rules the store enforces);
 *   * the six-digit code inputs (digits only, paste-friendly);
 *   * busy states on submit: label swaps, spinner runs, button disables — a
 *     slow form never looks dead and never double-submits;
 *   * the personalization wizard's stepper (Back/Next/Finish).
 *
 * Everything is progressive enhancement: each form also posts correctly with
 * JavaScript off (validation attributes + server checks carry the flow).
 */
(function () {
  "use strict";

  var EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
  var MIN_PASSWORD = 8;

  function fieldOf(form, input) {
    var err = form.querySelector('[data-error-for="' + input.id + '"]');
    return err || null;
  }

  function setInvalid(form, input, bad) {
    input.classList.toggle("invalid", !!bad);
    var err = fieldOf(form, input);
    if (err) err.classList.toggle("show", !!bad);
    if (bad) input.setAttribute("aria-invalid", "true");
    else input.removeAttribute("aria-invalid");
  }

  function check(form, input) {
    var kind = input.getAttribute("data-validate") || "";
    var value = input.value || "";
    var bad = false;
    if (kind === "name") bad = value.trim().length === 0;
    else if (kind === "email") bad = !EMAIL_RE.test(value.trim());
    else if (kind === "required") bad = value.length === 0;
    else if (kind === "password") {
      bad = value.length < MIN_PASSWORD || !/[A-Za-z]/.test(value) || !/[0-9]/.test(value);
    } else if (kind === "confirm") {
      var other = form.querySelector("#" + input.getAttribute("data-match"));
      bad = !other || value !== other.value || value.length === 0;
    }
    setInvalid(form, input, bad);
    return !bad;
  }

  /* The strength meter scores the same three rules the server enforces, so the
     client never declares "strong" what the server would reject. */
  function strengthOf(value) {
    var score = 0;
    if (value.length >= MIN_PASSWORD) score += 1;
    if (value.length >= 12) score += 1;
    if (/[A-Za-z]/.test(value) && /[0-9]/.test(value) && /[^A-Za-z0-9]/.test(value)) score += 1;
    return Math.min(score, 3);
  }

  function wireStrength(input) {
    var meter = document.getElementById(input.getAttribute("data-strength-meter"));
    var note = document.getElementById(input.getAttribute("data-strength-note"));
    if (!meter) return;
    function paint() {
      var value = input.value || "";
      var score = strengthOf(value);
      meter.setAttribute("data-score", String(score));
      if (note) {
        if (!value) note.textContent = "";
        else if (score <= 1) note.textContent = "Weak — add length, and mix letters with numbers.";
        else if (score === 2) note.textContent = "Good — a symbol or a few more characters makes it strong.";
        else note.textContent = "Strong.";
      }
    }
    input.addEventListener("input", paint);
    paint();
  }

  function busy(button) {
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

  window.OrbitBusy = { busy: busy };

  function wireForm(form) {
    var inputs = Array.prototype.slice.call(form.querySelectorAll("[data-validate]"));
    inputs.forEach(function (input) {
      input.addEventListener("blur", function () { if (input.value) check(form, input); });
      input.addEventListener("input", function () {
        if (input.classList.contains("invalid")) check(form, input);
        if (input.getAttribute("data-strength-meter")) wireStrengthIfNeeded(input);
      });
    });
    form.addEventListener("submit", function (event) {
      var firstBad = null;
      inputs.forEach(function (input) {
        if (!check(form, input) && !firstBad) firstBad = input;
      });
      if (firstBad) {
        event.preventDefault();
        firstBad.focus();
        return;
      }
      var button = form.querySelector('[type="submit"][data-busy-label]') || form.querySelector('[type="submit"]');
      busy(button);
    });
  }

  var strengthWired = typeof WeakSet !== "undefined" ? new WeakSet() : [];
  function wireStrengthIfNeeded(input) {
    if (strengthWired.has && strengthWired.has(input)) return;
    if (strengthWired.add) strengthWired.add(input);
    wireStrength(input);
  }

  /* The verification/reset code box: digits only, and a pasted code lands
     whole. Submit stays manual — you should get to read what you paste. */
  function wireCodeInput(input) {
    input.addEventListener("input", function () {
      var digits = (input.value || "").replace(/\D/g, "").slice(0, 6);
      if (digits !== input.value) input.value = digits;
    });
    input.addEventListener("paste", function (event) {
      var text = (event.clipboardData || window.clipboardData).getData("text") || "";
      var digits = text.replace(/\D/g, "").slice(0, 6);
      if (digits) {
        event.preventDefault();
        input.value = digits;
      }
    });
  }

  /* The personalization wizard: four panels, Back/Next/Finish, live dots and
     the "x of 4" line. Everything is optional, so Next never blocks. */
  function wireWizard(form) {
    var steps = Array.prototype.slice.call(form.querySelectorAll("[data-step]"));
    if (!steps.length) return;
    var dots = Array.prototype.slice.call(document.querySelectorAll("[data-dot]"));
    var back = document.getElementById("wizBack");
    var next = document.getElementById("wizNext");
    var finish = document.getElementById("wizFinish");
    var count = document.getElementById("wizCount");
    var current = 0;

    function show(index) {
      current = index;
      steps.forEach(function (step, i) { step.classList.toggle("active", i === index); });
      dots.forEach(function (dot, i) {
        dot.classList.toggle("active", i === index);
        dot.classList.toggle("done", i < index);
      });
      if (back) back.disabled = index === 0;
      if (next) next.style.display = index === steps.length - 1 ? "none" : "";
      if (finish) finish.style.display = index === steps.length - 1 ? "" : "none";
      if (count) count.textContent = (index + 1) + " of " + steps.length;
      var heading = steps[index].querySelector("h2");
      if (heading) {
        heading.setAttribute("tabindex", "-1");
        heading.focus({ preventScroll: true });
      }
    }

    if (back) back.addEventListener("click", function () { show(Math.max(0, current - 1)); });
    if (next) next.addEventListener("click", function () { show(Math.min(steps.length - 1, current + 1)); });
    form.addEventListener("submit", function () {
      var button = form.querySelector('[type="submit"][data-busy-label]');
      busy(button);
    });
    show(0);
  }

  function boot() {
    document.querySelectorAll("[data-auth-form]").forEach(function (form) {
      wireForm(form);
    });
    document.querySelectorAll("[data-strength-meter]").forEach(wireStrengthIfNeeded);
    document.querySelectorAll("[data-code-input]").forEach(wireCodeInput);
    document.querySelectorAll("[data-wizard]").forEach(wireWizard);
    // The demo OAuth buttons get the same honest busy affordance — the demo
    // completes instantly, but the affordance is part of the design review.
    document.querySelectorAll('[data-provider]').forEach(function (button) {
      button.addEventListener("click", function () { busy(button); });
    });
  }

  window.OrbitPublic = { boot: boot };
})();
