/* Approval review decisions — a client for the existing endpoints, nothing more.
 *
 * Approve runs exactly the two calls the screen promises, in order:
 *   POST /api/task/<id>/approve  {"approve": true}   (the gate is unchanged)
 *   POST /api/task/<id>/apply
 * Reject runs:
 *   POST /api/task/<id>/reject
 * and leaves files untouched.
 *
 * Every path ends visibly: a decision reloads the review into its decided
 * state, and a failure prints a line under the buttons instead of silently
 * doing nothing. Without JavaScript the page says so rather than pretending
 * the buttons work (see the <noscript> in approvals.html).
 */
(function () {
  var actions = document.getElementById("arActions");
  if (!actions) return;

  var approveBtn = document.getElementById("arApprove");
  var rejectBtn = document.getElementById("arReject");
  var applyBtn = document.getElementById("arApply");
  var confirm = document.getElementById("arConfirm");
  var errorLine = document.getElementById("arError");

  var approveUrl = actions.getAttribute("data-approve-url");
  var applyUrl = actions.getAttribute("data-apply-url");
  var rejectUrl = actions.getAttribute("data-reject-url");

  function fail(message) {
    if (!errorLine) return;
    errorLine.hidden = false;
    errorLine.textContent = message || "That decision did not go through. Nothing changed.";
  }

  function busy(on) {
    [approveBtn, rejectBtn, applyBtn].forEach(function (button) {
      if (button) button.disabled = on;
    });
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

  if (confirm && approveBtn) {
    confirm.addEventListener("change", function () {
      approveBtn.disabled = !confirm.checked;
    });
  }

  if (approveBtn) {
    approveBtn.addEventListener("click", function () {
      busy(true);
      if (errorLine) {
        errorLine.hidden = true;
        errorLine.textContent = "";
      }
      // The gate first: approve is only real with the literal flag, and the
      // endpoint refuses anything else with 400. Then apply — which is 403
      // until the approval landed. Both, in this order, every time.
      post(approveUrl, { approve: true })
        .then(function (result) {
          if (!result.ok) {
            throw new Error(
              (result.data && result.data.error) || "Your approval was not recorded."
            );
          }
          return post(applyUrl, {});
        })
        .then(function (result) {
          if (!result.ok) {
            throw new Error(
              (result.data && result.data.error) || "The draft is approved but was not applied."
            );
          }
          window.location = window.location.pathname + window.location.search;
        })
        .catch(function (err) {
          busy(false);
          if (confirm && approveBtn) approveBtn.disabled = !confirm.checked;
          fail(err && err.message);
        });
    });
  }

  if (applyBtn) {
    applyBtn.addEventListener("click", function () {
      busy(true);
      if (errorLine) {
        errorLine.hidden = true;
        errorLine.textContent = "";
      }
      post(applyUrl, {})
        .then(function (result) {
          if (!result.ok) {
            throw new Error(
              (result.data && result.data.error) || "The draft was not applied."
            );
          }
          window.location = window.location.pathname + window.location.search;
        })
        .catch(function (err) {
          busy(false);
          fail(err && err.message);
        });
    });
  }

  if (rejectBtn) {
    rejectBtn.addEventListener("click", function () {
      busy(true);
      if (errorLine) {
        errorLine.hidden = true;
        errorLine.textContent = "";
      }
      post(rejectUrl, {})
        .then(function (result) {
          if (!result.ok) {
            throw new Error(
              (result.data && result.data.error) || "Your rejection was not recorded."
            );
          }
          window.location = window.location.pathname + window.location.search;
        })
        .catch(function (err) {
          busy(false);
          fail(err && err.message);
        });
    });
  }
})();
