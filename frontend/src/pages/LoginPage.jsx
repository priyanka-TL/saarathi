import { useEffect, useState } from 'react';
// import { Link } from 'react-router-dom'; -- only used by the Register
// link below, disabled for now; see the auth-switch paragraph.
import { useNavigate } from 'react-router-dom';

import {
  extractBranding,
  extractMessage,
  extractToken,
  extractUserSummary,
  getBranding,
  loginWithOtp,
  loginWithPassword,
  sendLoginOtp,
} from '../api/elevateAuth';
import BrandLogo from '../components/sidebar/BrandLogo.jsx';
import { useAuth } from '../context/AuthContext.jsx';
import { AUTH_MODES as STATIC_AUTH_MODES } from '../config/env';
import { AUTH_MODE } from '../utils/authModes';

// No country picker in the form -- every account this POC talks to is +91,
// and ELEVATE's OTP/login calls still need the field, so it goes in the
// request body without ever being shown or editable.
const PHONE_CODE = '+91';

/**
 * Logs a user in directly against ELEVATE -- Saarthi's own backend never sees
 * a password or an OTP, only the JWT this produces (see api/elevateAuth.js).
 *
 * Two modes, matching what ELEVATE itself supports per tenant: password is a
 * single round trip, OTP is send-then-verify. WHICH modes are even offered is
 * config-driven, not a fixed pair -- `enabledModes` starts from the static
 * `AUTH_MODES` default (src/config/env.js) and is overridden by ELEVATE's own
 * `configuration.allowed_auth_mode` once branding loads (see the effect
 * below), so a tenant that only allows one mode never sees a tab for the
 * other, or a field ELEVATE would just reject.
 */
export default function LoginPage() {
  const { login } = useAuth();
  const navigate = useNavigate();

  const [enabledModes, setEnabledModes] = useState(STATIC_AUTH_MODES);
  const [mode, setMode] = useState(() =>
    STATIC_AUTH_MODES.includes(AUTH_MODE.PASSWORD) ? AUTH_MODE.PASSWORD : AUTH_MODE.OTP);
  const [identifier, setIdentifier] = useState('');
  const [password, setPassword] = useState('');
  const [otp, setOtp] = useState('');
  const [otpSent, setOtpSent] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  // Empty strings, never null -- '||' below falls back to the static
  // Saarthi name/logo while this is loading, and again if ELEVATE's
  // branding endpoint is unreachable or the tenant has none configured.
  const [branding, setBranding] = useState({ name: '', logoUrl: '' });

  useEffect(() => {
    let cancelled = false;
    getBranding().then((response) => {
      if (cancelled || !response.ok) return;
      const { name, logoUrl, allowedAuthModes } = extractBranding(response.data);
      setBranding({ name, logoUrl });
      // `[]` means the tenant declared nothing -- keep the static default
      // rather than overriding it with "no modes at all".
      if (allowedAuthModes.length) setEnabledModes(allowedAuthModes);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  const showPassword = enabledModes.includes(AUTH_MODE.PASSWORD);
  const showOtp = enabledModes.includes(AUTH_MODE.OTP);
  const showTabs = showPassword && showOtp;

  const switchMode = (nextMode) => {
    setMode(nextMode);
    setError('');
    setOtpSent(false);
    setOtp('');
    setPassword('');
  };

  // Corrects an already-selected mode that branding (loading asynchronously,
  // after the initial render) just invalidated -- e.g. defaulted to Password
  // before branding loaded, then the tenant turns out to be OTP-only.
  // Deliberately keyed on enabledModes alone: mode/showPassword are read for
  // their CURRENT value, not to re-run this on every keystroke that touches
  // unrelated state -- the correction only needs to fire when the enabled
  // set itself changes.
  useEffect(() => {
    if (!enabledModes.includes(mode)) {
      switchMode(showPassword ? AUTH_MODE.PASSWORD : AUTH_MODE.OTP);
    }
  }, [enabledModes]); // eslint-disable-line react-hooks/exhaustive-deps

  const handleSendOtp = async (event) => {
    event.preventDefault();
    if (!identifier.trim()) {
      setError('Enter your phone number first.');
      return;
    }
    setBusy(true);
    setError('');
    try {
      const response = await sendLoginOtp({ phone: identifier.trim(), phone_code: PHONE_CODE });
      if (!response.ok) {
        setError(extractMessage(response.data) || 'Could not send the OTP. Please try again.');
        return;
      }
      setOtpSent(true);
    } catch {
      setError('Network error. Please try again.');
    } finally {
      setBusy(false);
    }
  };

  const handleSubmit = async (event) => {
    event.preventDefault();
    if (!identifier.trim()) {
      setError('Enter your phone number or email.');
      return;
    }
    setBusy(true);
    setError('');
    try {
      const response =
        mode === AUTH_MODE.PASSWORD
          ? await loginWithPassword({ identifier: identifier.trim(), password })
          : await loginWithOtp({ identifier: identifier.trim(), otp, phone_code: PHONE_CODE });

      if (!response.ok) {
        setError(extractMessage(response.data) || 'Login failed. Please check your details and try again.');
        return;
      }

      const token = extractToken(response.data);
      if (!token) {
        setError('Login succeeded but no session token was returned. Please try again.');
        return;
      }

      login(token, extractUserSummary(response.data));
      navigate('/', { replace: true });
    } catch {
      setError('Network error. Please try again.');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="auth-page">
      <div className="auth-card">
        <div className="auth-brand">
          <div className="auth-brand-icon">
            {branding.logoUrl ? (
              // `.brand-logo` (root.css) already sizes/fits whatever image it's
              // given -- reused as-is rather than a second, identical rule.
              <img src={branding.logoUrl} alt={branding.name || 'Saarthi'} className="brand-logo" />
            ) : (
              <BrandLogo />
            )}
          </div>
          <h1>{branding.name || 'Saarthi'}</h1>
          <p>Sign in to continue</p>
        </div>

        {showTabs && (
          <div className="auth-mode-toggle" role="tablist" aria-label="Login method">
            <button
              type="button"
              role="tab"
              aria-selected={mode === AUTH_MODE.PASSWORD}
              className={mode === AUTH_MODE.PASSWORD ? 'auth-mode-btn active' : 'auth-mode-btn'}
              onClick={() => switchMode(AUTH_MODE.PASSWORD)}
            >
              Password
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={mode === AUTH_MODE.OTP}
              className={mode === AUTH_MODE.OTP ? 'auth-mode-btn active' : 'auth-mode-btn'}
              onClick={() => switchMode(AUTH_MODE.OTP)}
            >
              OTP
            </button>
          </div>
        )}

        <form className="auth-form" onSubmit={handleSubmit}>
          <label className="auth-field">
            {/* OTP mode is phone-only: ELEVATE's OTP-send endpoint
                (registrationOtp, shared by login and signup) takes
                phone/phone_code, no email variant. */}
            <span>{mode === AUTH_MODE.PASSWORD ? 'Phone or email' : 'Phone number'}</span>
            <input
              type="text"
              value={identifier}
              onChange={(e) => setIdentifier(e.target.value)}
              placeholder={mode === AUTH_MODE.PASSWORD ? '9995385076 or you@example.com' : '9995385076'}
              autoComplete="username"
              disabled={busy}
            />
          </label>

          {mode === AUTH_MODE.PASSWORD ? (
            <label className="auth-field">
              <span>Password</span>
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete="current-password"
                disabled={busy}
              />
            </label>
          ) : (
            <>
              {!otpSent ? (
                <button type="button" className="auth-secondary-btn" onClick={handleSendOtp} disabled={busy}>
                  {busy ? 'Sending…' : 'Send OTP'}
                </button>
              ) : (
                <label className="auth-field">
                  <span>OTP</span>
                  <input
                    type="text"
                    inputMode="numeric"
                    value={otp}
                    onChange={(e) => setOtp(e.target.value)}
                    autoComplete="one-time-code"
                    disabled={busy}
                  />
                </label>
              )}
            </>
          )}

          {error && <div className="auth-error">{error}</div>}

          {(mode === AUTH_MODE.PASSWORD || otpSent) && (
            <button type="submit" className="auth-submit-btn" disabled={busy}>
              {busy ? 'Signing in…' : 'Sign in'}
            </button>
          )}
        </form>

        {/* Registration is folded into this same screen for now, rather
            than a separate page -- see AppRoutes.jsx, where /register is
            disabled the same way. Re-enable both together.
        <p className="auth-switch">
          Don&rsquo;t have an account? <Link to="/register">Register</Link>
        </p>
        */}
      </div>
    </div>
  );
}
