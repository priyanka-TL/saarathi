/**
 * The URL layer had zero coverage, and it is the one place where a wrong
 * string breaks every request in the app at once.
 *
 * env.js resolves its values at MODULE LOAD, so each case has to reset the
 * module registry and re-import rather than reassign an export.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

async function loadEnv({ runtime, build } = {}) {
  vi.resetModules();

  if (runtime === undefined) {
    delete window.__APP_CONFIG__;
  } else {
    window.__APP_CONFIG__ = runtime;
  }

  for (const [key, value] of Object.entries(build ?? {})) {
    vi.stubEnv(key, value);
  }

  return import('../config/env');
}

beforeEach(() => {
  vi.stubEnv('APPLICATION_API_BASE_URL', '');
});

afterEach(() => {
  vi.unstubAllEnvs();
  delete window.__APP_CONFIG__;
});

describe('API_BASE_URL resolution', () => {
  it('is empty when nothing is configured anywhere', async () => {
    const env = await loadEnv();
    expect(env.API_BASE_URL).toBe('');
  });

  it('falls back to the build-time env when no runtime config exists', async () => {
    const env = await loadEnv({
      build: { APPLICATION_API_BASE_URL: 'http://127.0.0.1:8000/saarathi-service/api/' },
    });
    expect(env.API_BASE_URL).toBe('http://127.0.0.1:8000/saarathi-service/api/');
  });

  it('lets the runtime config override the build-time value', async () => {
    // This is the whole point of config.js: one built bundle, repointed after
    // the fact without a rebuild.
    const env = await loadEnv({
      runtime: { API_BASE_URL: 'https://prod.example.org/saarathi-service/api/' },
      build: { APPLICATION_API_BASE_URL: 'http://127.0.0.1:8000/saarathi-service/api/' },
    });
    expect(env.API_BASE_URL).toBe('https://prod.example.org/saarathi-service/api/');
  });

  it('ignores a blank runtime value and falls through to the build-time one', async () => {
    // The shipped public/config.js has an empty string; it must be a no-op.
    const env = await loadEnv({
      runtime: { API_BASE_URL: '   ' },
      build: { APPLICATION_API_BASE_URL: 'http://127.0.0.1:8000/saarathi-service/api/' },
    });
    expect(env.API_BASE_URL).toBe('http://127.0.0.1:8000/saarathi-service/api/');
  });

  it('trims surrounding whitespace from either source', async () => {
    const env = await loadEnv({
      build: { APPLICATION_API_BASE_URL: '  http://127.0.0.1:8000/api/  ' },
    });
    expect(env.API_BASE_URL).toBe('http://127.0.0.1:8000/api/');
  });
});
