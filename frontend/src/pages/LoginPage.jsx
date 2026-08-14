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
 * Whether what the user typed is an email address rather than a phone number.
 *
 * ONE FIELD ACCEPTS BOTH, in both modes, so something has to decide which key
 * the OTP request carries -- see `sendLoginOtp`.
 *
 * `@` AND NOT A VALIDATING REGEX, deliberately. This is a routing question, not
 * a validation one: the only job is telling two shapes apart, and a phone number
 * never contains an `@`. A stricter pattern buys nothing here and can only
 * misfire in the expensive direction -- rejecting a real address ELEVATE would
 * have accepted, leaving the user unable to log in with no way to tell why.
 * ELEVATE validates the address itself and its answer is the one that matters.
 */
function isEmailIdentifier(value) {
  return value.includes('@');
}

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
    const value = identifier.trim();
    if (!value) {
      setError('Enter your phone number or email first.');
      return;
    }
    setBusy(true);
    setError('');
    try {
      // An OTP goes to a phone OR an address, and which one decides the whole
      // request shape -- `phone_code` must not travel with an email. The
      // branch lives here rather than in the API module because this is the
      // only place that knows the single field holds either.
      const response = await sendLoginOtp(
        isEmailIdentifier(value)
          ? { email: value }
          : { phone: value, phone_code: PHONE_CODE },
      );
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
      const value = identifier.trim();
      // VERIFY THE SAME SHAPE THE OTP WAS MINTED AGAINST. `handleSendOtp`
      // sends an address as `email` with no `phone_code`; carrying one back
      // here would ask ELEVATE to verify an email against a phone-shaped
      // request, and the half that fails would be the half the user sees.
      const response =
        mode === AUTH_MODE.PASSWORD
          ? await loginWithPassword({ identifier: value, password })
          : await loginWithOtp({
              identifier: value,
              otp,
              ...(isEmailIdentifier(value) ? {} : { phone_code: PHONE_CODE }),
            });

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
          {/* NAMES WHAT YOU SIGN IN WITH. One line for both modes, because
              both accept either: password logs in on an identifier, and OTP is
              minted to a phone or an address depending on which was typed
              (`sendLoginOtp`). */}
          <p>Sign in to continue with your phone number or email</p>
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
            {/* THE SAME IN BOTH MODES, because both accept either. The OTP
                endpoint (registrationOtp, shared by login and signup) takes an
                email as readily as a phone -- `sendLoginOtp` picks the key off
                what was typed. This label used to narrow to "Phone number" in
                OTP mode and turned a supported way of signing in into one the
                form appeared to forbid. */}
            <span>Phone number / email</span>
            <input
              type="text"
              value={identifier}
              onChange={(e) => setIdentifier(e.target.value)}
              // MASKED, AND SYNTHETIC. Two separate reasons, both load-bearing:
              //
              //   masked     a placeholder is a hint, not a specimen to copy.
              //              Showing ten complete digits invited exactly that.
              //   synthetic  this started life as a real person's mobile
              //              number. A placeholder ships in a bundle every
              //              visitor downloads, so it must never be anyone's.
              //
              // The visible half still carries the shape a valid entry has -- an
              // Indian mobile, ten digits, leading 9 -- which is what the hint
              // is actually for. Keep both properties if this is ever changed.
              placeholder="98765***** or you@example.com"
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
