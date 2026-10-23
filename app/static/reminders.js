/* Reminders — a client for the heartbeat's existing endpoints, nothing more.
 *
 * Check now  → POST /api/heartbeat/check          (the real checks, on demand)
 * Mark done  → POST /api/heartbeat/notices/<id>/dismiss
 * Pause      → POST /api/heartbeat/pause {"paused": <bool>}
 *
 * Every path ends visibly: the page reloads into the state the API reported
 * (including "Checked just now · N new reminders"), and a failure prints a
 * line under the header instead of silently doing nothing. Snooze and New
 * reminder are disabled buttons — nothing here pretends they work.
 */
(function () {
  var errorLine = document.getElementById("rmError");

  function fail(message) {
    if (!errorLine) return;
    errorLine.hidden = false;
    errorLine.textContent = message || "That did not go through. Nothing changed.";
  }

  function clearError() {
    if (!errorLine) return;
    errorLine.hidden = true;
    errorLine.textContent = "";
  }

  function post(url, body) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    }).then(function (response) {
      return response
        .json()
        .catch(function () {
          return {};
        })
        .then(function (data) {
          return { ok: response.ok, status: response.status, data: data };
        });
    });
  }

  var checkBtn = document.getElementById("rmCheck");
  if (checkBtn) {
    checkBtn.addEventListener("click", function () {
      clearError();
      checkBtn.disabled = true;
      post("/api/heartbeat/check", {})
        .then(function (result) {
          if (!result.ok) {
            throw new Error((result.data && result.data.error) || "The check did not run.");
          }
          var fired = result.data.fired || [];
          var flag = result.data.status === "paused" ? "paused" : String(fired.length);
          var here = new URL(window.location.href);
          here.searchParams.set("checked", flag);
          window.location = here.toString();
        })
        .catch(function (err) {
          checkBtn.disabled = false;
          fail(err && err.message);
        });
    });
  }

  document.querySelectorAll("[data-dismiss]").forEach(function (button) {
    button.addEventListener("click", function () {
      clearError();
      button.disabled = true;
      var id = button.getAttribute("data-dismiss");
      post("/api/heartbeat/notices/" + encodeURIComponent(id) + "/dismiss", {})
        .then(function (result) {
          if (!result.ok) {
            throw new Error(
              (result.data && result.data.error) || "That reminder could not be completed."
            );
          }
          window.location = window.location.pathname + window.location.search;
        })
        .catch(function (err) {
          button.disabled = false;
          fail(err && err.message);
        });
    });
  });

  var pause = document.getElementById("rmPause");
  if (pause) {
    pause.addEventListener("change", function () {
      clearError();
      post("/api/heartbeat/pause", { paused: pause.checked })
        .then(function (result) {
          if (!result.ok) {
            throw new Error((result.data && result.data.error) || "The pause did not change.");
          }
          window.location = window.location.pathname + window.location.search;
        })
        .catch(function (err) {
          pause.checked = !pause.checked;
          fail(err && err.message);
        });
    });
  }
})();
