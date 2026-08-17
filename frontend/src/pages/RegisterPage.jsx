import { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';

import { extractMessage, register, sendRegistrationOtp } from '../api/elevateAuth';
import BrandLogo from '../components/sidebar/BrandLogo.jsx';

// No country picker in the form -- every account this POC talks to is +91,
// and ELEVATE's OTP/register calls still need the field, so it goes in the
// request body without ever being shown or editable.
const PHONE_CODE = '+91';

/**
 * Creates a new ELEVATE account -- directly against ELEVATE, same as login
 * (see api/elevateAuth.js). Saarthi's backend has no register endpoint and
 * never sees the OTP: a new user's identity is entirely ELEVATE's to own.
 *
 * Send-OTP-then-create, mirroring ELEVATE's own two-step signup. On success
 * this routes to /login rather than logging the new user in directly --
 * simpler, and it means one code path (LoginPage) is the only place that ever
 * has to turn an ELEVATE response into a Saarthi session.
 */
export default function RegisterPage() {
  const navigate = useNavigate();

  const [name, setName] = useState('');
  const [phone, setPhone] = useState('');
  const [otp, setOtp] = useState('');
  const [password, setPassword] = useState('');
  const [otpSent, setOtpSent] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [done, setDone] = useState(false);

  const handleSendOtp = async (event) => {
    event.preventDefault();
    if (!phone.trim()) {
      setError('Enter your phone number first.');
      return;
    }
    setBusy(true);
    setError('');
    try {
      const response = await sendRegistrationOtp({ phone: phone.trim(), phone_code: PHONE_CODE });
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
    setBusy(true);
    setError('');
    try {
      const response = await register({
        phone: phone.trim(), phone_code: PHONE_CODE, otp, name: name.trim(),
        password: password || undefined,
      });
      if (!response.ok) {
        setError(extractMessage(response.data) || 'Registration failed. Please try again.');
        return;
      }
      setDone(true);
    } catch {
      setError('Network error. Please try again.');
    } finally {
      setBusy(false);
    }
  };

  if (done) {
    return (
      <div className="auth-page">
        <div className="auth-card">
          <div className="auth-brand">
            <div className="auth-brand-icon">
              <BrandLogo />
            </div>
            <h1>Saarthi</h1>
          </div>
          <p className="auth-success">Account created. You can now sign in.</p>
          <button type="button" className="auth-submit-btn" onClick={() => navigate('/login', { replace: true })}>
            Go to login
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="auth-page">
      <div className="auth-card">
        <div className="auth-brand">
          <div className="auth-brand-icon">
            <BrandLogo />
          </div>
          <h1>Saarthi</h1>
          <p>Create your account</p>
        </div>

        <form className="auth-form" onSubmit={handleSubmit}>
          <label className="auth-field">
            <span>Name</span>
            <input
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              autoComplete="name"
              disabled={busy}
            />
          </label>

          <label className="auth-field">
            <span>Phone number</span>
            <input
              type="tel"
              value={phone}
              onChange={(e) => setPhone(e.target.value)}
              autoComplete="tel"
              disabled={busy || otpSent}
            />
          </label>

          {!otpSent ? (
            <button type="button" className="auth-secondary-btn" onClick={handleSendOtp} disabled={busy}>
              {busy ? 'Sending…' : 'Send OTP'}
            </button>
          ) : (
            <>
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

              <label className="auth-field">
                <span>Password (optional)</span>
                <input
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  autoComplete="new-password"
                  disabled={busy}
                />
              </label>
            </>
          )}

          {error && <div className="auth-error">{error}</div>}

          {otpSent && (
            <button type="submit" className="auth-submit-btn" disabled={busy}>
              {busy ? 'Creating account…' : 'Create account'}
            </button>
          )}
        </form>

        <p className="auth-switch">
          Already have an account? <Link to="/login">Sign in</Link>
        </p>
      </div>
    </div>
  );
}
