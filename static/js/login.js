/* ── Login page controller ────────────────────────────────────────────────
 * Purpose:
 *   1. Submit { email, password } to POST /api/auth/login.
 *   2. On 200: redirect to '/'.
 *   3. On 400/401: show the server's error message inline.
 *
 * NOTE: The shared password is NEVER hard-coded here.  It is validated
 * server-side by services/auth_service.verify_shared_password.  This
 * script does only lightweight client-side format hints (empty checks,
 * trim) — backend validation is authoritative. */

(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);

  const form     = $('login-form');
  const emailEl  = $('login-email');
  const passEl   = $('login-password');
  const errEl    = $('login-error');
  const btn      = $('login-submit-btn');
  const btnLabel = btn.querySelector('.login-submit-label');

  function showError(msg) {
    errEl.textContent = msg || 'Invalid email or password.';
    errEl.style.display = '';
  }

  function clearError() {
    errEl.textContent = '';
    errEl.style.display = 'none';
  }

  function setLoading(on) {
    btn.disabled = on;
    emailEl.disabled = on;
    passEl.disabled = on;
    btn.classList.toggle('is-loading', on);
    btnLabel.textContent = on ? 'Signing in…' : 'Login';
  }

  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    clearError();

    const email = (emailEl.value || '').trim();
    const password = passEl.value || '';

    // Very light client-side check — the backend is the source of truth.
    if (!email) { showError('Please enter your company email.'); emailEl.focus(); return; }
    if (!password) { showError('Please enter the application password.'); passEl.focus(); return; }

    setLoading(true);
    try {
      const res = await fetch('/api/auth/login', {
        method:      'POST',
        credentials: 'same-origin',
        headers:     { 'Content-Type': 'application/json' },
        body:        JSON.stringify({ email, password }),
      });
      if (res.ok) {
        // Server has set the HttpOnly session cookie; hand off to the app.
        window.location.assign('/');
        return;
      }
      // Try to parse a clean message; fall back to a generic one.
      let msg = 'Invalid email or password.';
      try {
        const body = await res.json();
        if (body && typeof body.detail === 'string' && body.detail.trim()) {
          msg = body.detail;
        }
      } catch (_) { /* ignore JSON errors */ }
      showError(msg);
      passEl.value = '';
      passEl.focus();
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
