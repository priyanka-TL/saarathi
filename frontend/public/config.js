/**
 * RUNTIME configuration. Overwritten at deploy time; NOT bundled.
 *
 * Vite copies public/ to the dist root verbatim, so this file sits next to
 * index.html in the built artifact and is loaded before the app. That is what
 * lets ONE build serve development, QA and production: point it at the right
 * backend and reload -- no rebuild.
 *
 *   API_BASE_URL       complete backend base URL, INCLUDING the service prefix
 *                      and /api/, e.g. 'https://qa.example.org/saarathi-service/api/'.
 *                      '' => same origin as the page (relative requests).
 *   ELEVATE_BASE_URL   ELEVATE's user service, e.g.
 *                      'https://qa.elevate-apis.shikshalokam.org'. The login
 *                      page talks to this directly, never through Saarthi.
 *   ELEVATE_TENANT_ID  the `tenantid` header value login/register send.
 *   AUTH_MODES         STATIC default for which login modes show
 *                      ("otp", "password", or "password,otp"). Only the
 *                      fallback -- ELEVATE's own branding response overrides
 *                      it once loaded. See src/utils/authModes.js.
 *   PROFILE_POPUP_ENABLED
 *                      'false'/'0'/'off'/'no' stops the profile form popping
 *                      up after login when mandatory fields are missing.
 *                      DEFAULT ON. The sidebar's Profile section and its
 *                      Update button stay available either way -- this only
 *                      governs the unprompted dialog.
 *
 * A blank value falls through to the build-time APPLICATION_* value from
 * .env; deleting the file entirely is also safe (the app just uses the
 * build-time values). See src/config/env.js.
 */
window.__APP_CONFIG__ = {
  API_BASE_URL: '',
  ELEVATE_BASE_URL: '',
  ELEVATE_TENANT_ID: '',
  AUTH_MODES: '',
  PROFILE_POPUP_ENABLED: '',
};
