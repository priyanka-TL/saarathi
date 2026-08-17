import { beforeEach, describe, expect, it } from 'vitest';

import { http } from '../api/http';
import { STORAGE_KEYS } from '../constants';

/**
 * The two auth-related interceptors on the shared Saarthi-backend client:
 * attaching the stored token, and clearing it on a 401. A custom `adapter`
 * per call stands in for the network -- axios still runs every interceptor
 * around it, so this exercises the real request/response chain rather than
 * a re-implementation of it.
 */
function fakeAdapter(status) {
  return (config) => Promise.resolve({ data: {}, status, statusText: '', headers: {}, config });
}

function authHeader(response) {
  const headers = response.config.headers;
  return typeof headers.get === 'function' ? headers.get('Authorization') : headers.Authorization;
}

beforeEach(() => {
  localStorage.clear();
});

describe('http.js: auth interceptors', () => {
  it('attaches no Authorization header when logged out', async () => {
    const response = await http.get('/x', { adapter: fakeAdapter(200) });
    expect(authHeader(response)).toBeFalsy();
  });

  it('attaches Authorization: Bearer <token> when a token is stored', async () => {
    localStorage.setItem(STORAGE_KEYS.authToken, 'jwt-1');
    const response = await http.get('/x', { adapter: fakeAdapter(200) });
    expect(authHeader(response)).toBe('Bearer jwt-1');
  });

  it('reads the token fresh on every request, not once at load', async () => {
    localStorage.setItem(STORAGE_KEYS.authToken, 'jwt-1');
    await http.get('/x', { adapter: fakeAdapter(200) });

    localStorage.setItem(STORAGE_KEYS.authToken, 'jwt-2');
    const second = await http.get('/x', { adapter: fakeAdapter(200) });
    expect(authHeader(second)).toBe('Bearer jwt-2');
  });

  it('clears the stored token on a 401 response', async () => {
    localStorage.setItem(STORAGE_KEYS.authToken, 'jwt-1');
    await http.get('/x', { adapter: fakeAdapter(401) });
    expect(localStorage.getItem(STORAGE_KEYS.authToken)).toBeNull();
  });

  it('leaves the stored token alone on a non-401 response', async () => {
    localStorage.setItem(STORAGE_KEYS.authToken, 'jwt-1');
    await http.get('/x', { adapter: fakeAdapter(200) });
    expect(localStorage.getItem(STORAGE_KEYS.authToken)).toBe('jwt-1');
  });

  it('applies both interceptors to PATCH, not only GET/POST', async () => {
    // PATCH arrived with the profile update. Both interceptors are declared
    // method-agnostically, which is what let `patch` be a one-liner -- this
    // pins that so a future method-specific branch cannot quietly exempt it.
    localStorage.setItem(STORAGE_KEYS.authToken, 'jwt-1');

    const response = await http.patch('/x', { role: 'Teacher' }, { adapter: fakeAdapter(200) });
    expect(authHeader(response)).toBe('Bearer jwt-1');

    await http.patch('/x', {}, { adapter: fakeAdapter(401) });
    expect(localStorage.getItem(STORAGE_KEYS.authToken)).toBeNull();
  });
});
