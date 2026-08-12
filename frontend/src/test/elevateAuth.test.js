import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

/**
 * The one bug this guards against: an unconfigured ELEVATE_BASE_URL used to
 * silently send every login/OTP/register call to the SPA's OWN origin
 * (axios resolves an empty baseURL as relative-to-page) -- observed live as
 * a "Send OTP" click POSTing to http://localhost:5173/user/v1/account/....
 *
 * env.js resolves ELEVATE_BASE_URL at MODULE LOAD, so each case resets the
 * module registry and re-imports rather than reassigning an export -- same
 * technique as env.test.js.
 */
async function loadElevateAuth({ baseUrl } = {}) {
  vi.resetModules();
  vi.stubEnv('APPLICATION_ELEVATE_BASE_URL', baseUrl ?? '');
  vi.stubEnv('APPLICATION_ELEVATE_TENANT_ID', 'saarthi');
  delete window.__APP_CONFIG__;
  return import('../api/elevateAuth');
}

beforeEach(() => {
  vi.stubEnv('APPLICATION_API_BASE_URL', '');
});

afterEach(() => {
  vi.unstubAllEnvs();
  delete window.__APP_CONFIG__;
});

describe('elevateAuth: base URL guard', () => {
  it('refuses to call when ELEVATE_BASE_URL is unconfigured, rather than defaulting to same-origin', async () => {
    const { sendLoginOtp } = await loadElevateAuth({ baseUrl: '' });

    const result = await sendLoginOtp({ phone: '9995385076', phone_code: '+91' });

    expect(result.ok).toBe(false);
    expect(result.data.message).toMatch(/ELEVATE_BASE_URL is not configured/);
  });

  it('names the fix so a misconfigured deployment is not left guessing', async () => {
    const { loginWithPassword } = await loadElevateAuth({ baseUrl: '' });

    const result = await loginWithPassword({ identifier: 'x', password: 'y' });

    expect(result.data.message).toMatch(/APPLICATION_ELEVATE_BASE_URL/);
  });
});
