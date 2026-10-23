/* Settings — the two things that live in this browser, wired for real.
 *
 * Theme choice: "light" | "dark" | "system" saved to localStorage
 * (orbit.theme), applied to <html data-theme> now. "System" resolves at
 * each paint through the shared pre-paint script, so the device setting
 * wins until the person picks again.
 *
 * Pause reminders: POST /api/heartbeat/pause {"paused": <bool>} — the real
 * switch, the same endpoint the Reminders screen uses. A failure prints a
 * line instead of leaving the checkbox lying.
 *
 * Everything else on the page is a real server-side form.
 */
(function () {
  var THEME_KEY = "orbit.theme";

  function resolved(choice) {
    if (choice === "light" || choice === "dark") return choice;
    var dark =
      window.matchMedia &&
      window.matchMedia("(prefers-color-scheme: dark)").matches;
    return dark ? "dark" : "light";
  }

  function paintChoices() {
    var saved = null;
    try {
      saved = window.localStorage.getItem(THEME_KEY);
    } catch (_error) {
      saved = null;
    }
    var choice = saved === "light" || saved === "dark" ? saved : "system";
    document.querySelectorAll("[data-theme-choice]").forEach(function (button) {
      button.setAttribute(
        "aria-pressed",
        button.getAttribute("data-theme-choice") === choice ? "true" : "false"
      );
    });
    return choice;
  }

  document.querySelectorAll("[data-theme-choice]").forEach(function (button) {
    button.addEventListener("click", function () {
      var choice = button.getAttribute("data-theme-choice");
      try {
        window.localStorage.setItem(THEME_KEY, choice);
      } catch (_error) {
        // A blocked store: the appearance still applies for this page.
      }
      document.documentElement.dataset.theme = resolved(choice);
      paintChoices();
    });
  });
  paintChoices();

  var pause = document.getElementById("stPause");
  var errorLine = document.getElementById("stError");
  if (pause) {
    pause.addEventListener("change", function () {
      if (errorLine) {
        errorLine.hidden = true;
        errorLine.textContent = "";
      }
      fetch("/api/heartbeat/pause", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ paused: pause.checked }),
      })
        .then(function (response) {
          return response
            .json()
            .catch(function () {
              return {};
            })
            .then(function (data) {
              return { ok: response.ok, data: data };
            });
        })
        .then(function (result) {
          if (!result.ok) {
            throw new Error(
              (result.data && result.data.error) || "The pause did not change."
            );
          }
        })
        .catch(function (err) {
          pause.checked = !pause.checked;
          if (errorLine) {
            errorLine.hidden = false;
            errorLine.textContent = (err && err.message) || "That did not go through.";
          }
        });
    });
  }
})();
