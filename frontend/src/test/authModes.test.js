import { describe, expect, it } from 'vitest';

import { AUTH_MODE, DEFAULT_AUTH_MODES, parseAuthModes } from '../utils/authModes';

describe('parseAuthModes', () => {
  it('parses a comma-separated string', () => {
    expect(parseAuthModes('password,otp')).toEqual(['password', 'otp']);
  });

  it('accepts an array (ELEVATE\'s own JSON shape)', () => {
    expect(parseAuthModes(['otp'])).toEqual(['otp']);
  });

  it('trims whitespace and lower-cases', () => {
    expect(parseAuthModes(' Password , OTP ')).toEqual(['password', 'otp']);
  });

  it('drops unrecognised entries rather than keeping them as opaque strings', () => {
    expect(parseAuthModes('password,sms,otp')).toEqual(['password', 'otp']);
  });

  it('dedupes', () => {
    expect(parseAuthModes('otp,otp,password')).toEqual(['otp', 'password']);
  });

  it('returns [] for empty, null, undefined or entirely-unrecognised input', () => {
    expect(parseAuthModes('')).toEqual([]);
    expect(parseAuthModes(null)).toEqual([]);
    expect(parseAuthModes(undefined)).toEqual([]);
    expect(parseAuthModes('sms,email')).toEqual([]);
    expect(parseAuthModes([])).toEqual([]);
  });

  it('has no opinion on defaults -- that is the caller\'s job', () => {
    expect(parseAuthModes('garbage')).toEqual([]);
    expect(DEFAULT_AUTH_MODES).toEqual([AUTH_MODE.PASSWORD, AUTH_MODE.OTP]);
  });
});
