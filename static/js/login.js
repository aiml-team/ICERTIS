/* ── Login page controller ────────────────────────────────────────────────
 * Purpose:
 *   1. Basic client-side email-format validation.
 *   2. Submit { email } to POST /api/auth/login (email-only, no password).
 *   3. On 200: redirect to '/'.
 *   4. On 400/401: show the server's error message inline.
 *
 * Password-based login was removed at product request — this internal app
 * now authenticates by company email alone.  Backend still enforces the
 * company-domain check; this script only does a lightweight format hint
 * so users get instant feedback before the round-trip. */

(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);

  const form     = $('login-form');
  const emailEl  = $('login-email');
  const errEl    = $('login-error');
  const btn      = $('login-submit-btn');
  const btnLabel = btn.querySelector('.login-submit-label');

  // Lightweight RFC-5322-ish check.  Backend is authoritative.
  const EMAIL_RE = /^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$/;

  function showError(msg) {
    errEl.textContent = msg || 'Unable to sign in. Please try again.';
    errEl.style.display = '';
  }

  function clearError() {
    errEl.textContent = '';
    errEl.style.display = 'none';
  }

  function setLoading(on) {
    btn.disabled = on;
    emailEl.disabled = on;
    btn.classList.toggle('is-loading', on);
    btnLabel.textContent = on ? 'Signing in…' : 'Login';
  }

  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    clearError();

    const email = (emailEl.value || '').trim();

    // Basic client-side validation — the backend is the source of truth.
    if (!email) {
      showError('Please enter your company email.');
      emailEl.focus();
      return;
    }
    if (!EMAIL_RE.test(email)) {
      showError('Please enter a valid email address.');
      emailEl.focus();
      return;
    }

    setLoading(true);
    try {
      const res = await fetch('/api/auth/login', {
        method:      'POST',
        credentials: 'same-origin',
        headers:     { 'Content-Type': 'application/json' },
        body:        JSON.stringify({ email }),
      });
      if (res.ok) {
        // Server has set the HttpOnly session cookie; hand off to the app.
        window.location.assign('/');
        return;
      }
      // Try to parse a clean message; fall back to a generic one.
      let msg = 'Unable to sign in. Please try again.';
      try {
        const body = await res.json();
        if (body && typeof body.detail === 'string' && body.detail.trim()) {
          msg = body.detail;
        }
      } catch (_) { /* ignore JSON errors */ }
      showError(msg);
      emailEl.focus();
    } catch (_err) {
      showError('Unable to sign in right now. Please try again.');
    } finally {
      setLoading(false);
    }
  });

  // Autofocus the email field on load.
  window.addEventListener('DOMContentLoaded', () => {
    try { emailEl.focus(); } catch (_) { /* noop */ }
  });
})();
