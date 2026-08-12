import axios from 'axios';

import { API_BASE_URL } from '../config/env';
import { readAuthToken, writeAuth } from '../utils/storage';

/**
 * The single axios instance every API module uses.
 *
 * `validateStatus: () => true` is REQUIRED, not a shortcut. The original
 * `fetch` code never threw on an HTTP status, and the app depends on that in
 * two ways:
 *
 *   * 202 is a normal, expected outcome on /api/sessions/{id}/report and
 *     /api/sessions/{id}/resume, and carries a body that must be read.
 *   * 4xx/5xx bodies carry `error_code`, which drives the retry UI --
 *     UPSTREAM_TIMEOUT renders a recovery button, everything else renders a
 *     plain system message.
 *
 * An interceptor that rejected on non-2xx would collapse both into a thrown
 * error and lose the body.
 *
 * No X-Request-ID is sent: the original client never sent one, and the server
 * generates one per request anyway.
 */
export const http = axios.create({
  baseURL: API_BASE_URL,
  headers: { 'Content-Type': 'application/json' },
  validateStatus: () => true,
});

/**
 * Attaches the logged-in user's ELEVATE token to every request, read fresh
 * from storage each time (not captured once at module load) so a login or
 * logout in the same tab takes effect on the very next call.
 */
http.interceptors.request.use((config) => {
  const token = readAuthToken();
  if (token) {
    config.headers = { ...config.headers, Authorization: `Bearer ${token}` };
  }
  return config;
});

/**
 * A 401 means the token Saarthi validated is gone or rejected -- clear it so
 * the next render's RequireAuth redirects to /login, rather than looping the
 * same dead token onto every subsequent call.
 */
http.interceptors.response.use((response) => {
  if (response.status === 401) writeAuth(null, null);
  return response;
});

/**
 * Normalised result shape: `{ status, ok, data }`.
 *
 * `data` is null when the response had no parseable body. A transport-level
 * failure (server down, DNS, CORS) throws, and callers translate that into the
 * "Network error. Please try again." system message the original showed.
 */
export async function request(config) {
  const response = await http.request(config);
  return {
    status: response.status,
    ok: response.status >= 200 && response.status < 300,
    data: response.data ?? null,
  };
}

export const get = (url, config) => request({ ...config, method: 'GET', url });
export const post = (url, data, config) => request({ ...config, method: 'POST', url, data });
