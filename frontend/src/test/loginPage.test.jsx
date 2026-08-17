import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

// LoginPage reads AUTH_MODES from config/env.js directly (for the static
// default, before branding loads) -- mocked to a fixed value rather than
// left to resolve the real frontend/.env, which would make these tests
// depend on whatever a developer's local AUTH_MODES happens to be set to.
// The dynamic, branding-driven override is exercised via extractBranding's
// mock below instead, which is the actual behaviour under test.
vi.mock('../config/env', () => ({ AUTH_MODES: ['password', 'otp'] }));

vi.mock('../api/elevateAuth', () => ({
  loginWithPassword: vi.fn(),
  loginWithOtp: vi.fn(),
  sendLoginOtp: vi.fn(),
  extractToken: vi.fn((data) => data?.token ?? ''),
  extractMessage: vi.fn((data) => data?.message ?? ''),
  extractUserSummary: vi.fn((data) => data?.user ?? null),
  // Defaults to "no branding" so every other test renders the static
  // Saarthi name/logo and both tabs (from the config/env mock above), same
  // as a real unreachable-ELEVATE fallback.
  getBranding: vi.fn().mockResolvedValue({ ok: false, status: 0, data: null }),
  extractBranding: vi.fn(() => ({ name: '', logoUrl: '', allowedAuthModes: [] })),
}));

import {
  extractBranding, getBranding, loginWithOtp, loginWithPassword, sendLoginOtp,
} from '../api/elevateAuth';
import { AuthProvider, useAuth } from '../context/AuthContext.jsx';
import LoginPage from '../pages/LoginPage.jsx';

/**
 * The login page never talks to Saarthi's own backend -- only to ELEVATE,
 * directly (see api/elevateAuth.js, mocked here). These tests are about the
 * two modes and what a successful login does to AuthContext, not about
 * ELEVATE's own request/response shapes.
 */
function Probe() {
  const { isAuthenticated, user } = useAuth();
  return <div data-testid="probe">{isAuthenticated ? `in:${user?.name}` : 'out'}</div>;
}

function renderLogin() {
  return render(
    <MemoryRouter initialEntries={['/login']}>
      <AuthProvider>
        <LoginPage />
        <Probe />
      </AuthProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
});

describe('LoginPage: password mode', () => {
  it('logs in and updates AuthContext on success', async () => {
    loginWithPassword.mockResolvedValue({
      ok: true, status: 200, data: { token: 'jwt-1', user: { name: 'Asha' } },
    });
    const user = userEvent.setup();
    renderLogin();

    await user.type(screen.getByLabelText('Phone number / email'), '9876543210');
    await user.type(screen.getByLabelText('Password'), 'hunter2');
    await user.click(screen.getByRole('button', { name: 'Sign in' }));

    await waitFor(() => expect(screen.getByTestId('probe')).toHaveTextContent('in:Asha'));
    expect(loginWithPassword).toHaveBeenCalledWith({ identifier: '9876543210', password: 'hunter2' });
  });

  it('shows ELEVATE\'s own error message on failure', async () => {
    loginWithPassword.mockResolvedValue({
      ok: false, status: 400, data: { message: 'Invalid credentials' },
    });
    const user = userEvent.setup();
    renderLogin();

    await user.type(screen.getByLabelText('Phone number / email'), '9876543210');
    await user.type(screen.getByLabelText('Password'), 'wrong');
    await user.click(screen.getByRole('button', { name: 'Sign in' }));

    expect(await screen.findByText('Invalid credentials')).toBeInTheDocument();
    expect(screen.getByTestId('probe')).toHaveTextContent('out');
  });
});

describe('LoginPage: OTP mode', () => {
  it('sends an OTP, then logs in with it', async () => {
    sendLoginOtp.mockResolvedValue({ ok: true, status: 200, data: {} });
    loginWithOtp.mockResolvedValue({
      ok: true, status: 200, data: { token: 'jwt-2', user: { name: 'Rohan' } },
    });
    const user = userEvent.setup();
    renderLogin();

    await user.click(screen.getByRole('tab', { name: 'OTP' }));
    await user.type(screen.getByLabelText('Phone number / email'), '9876543210');
    await user.click(screen.getByRole('button', { name: 'Send OTP' }));

    expect(sendLoginOtp).toHaveBeenCalledWith({ phone: '9876543210', phone_code: '+91' });
    await screen.findByLabelText('OTP');

    await user.type(screen.getByLabelText('OTP'), '123456');
    await user.click(screen.getByRole('button', { name: 'Sign in' }));

    await waitFor(() => expect(screen.getByTestId('probe')).toHaveTextContent('in:Rohan'));
    expect(loginWithOtp).toHaveBeenCalledWith({
      identifier: '9876543210', otp: '123456', phone_code: '+91',
    });
  });

  it('sends the OTP to an EMAIL when one is typed, not to a phone', async () => {
    /**
     * The single field accepts either, and which was typed decides the request
     * shape: an address must travel as `email` with no `phone_code` beside it,
     * or ELEVATE reads the call as a phone lookup that happens to be malformed.
     */
    sendLoginOtp.mockResolvedValue({ ok: true, status: 200, data: {} });
    const user = userEvent.setup();
    renderLogin();

    await user.click(screen.getByRole('tab', { name: 'OTP' }));
    await user.type(screen.getByLabelText('Phone number / email'), 'asha@example.com');
    await user.click(screen.getByRole('button', { name: 'Send OTP' }));

    expect(sendLoginOtp).toHaveBeenCalledWith({ email: 'asha@example.com' });
    await screen.findByLabelText('OTP');

    // And the VERIFY must match the send: no phone_code on an email.
    loginWithOtp.mockResolvedValue({
      ok: true, status: 200, data: { token: 'jwt-3', user: { name: 'Asha' } },
    });
    await user.type(screen.getByLabelText('OTP'), '654321');
    await user.click(screen.getByRole('button', { name: 'Sign in' }));

    expect(loginWithOtp).toHaveBeenCalledWith({
      identifier: 'asha@example.com', otp: '654321',
    });
  });
});

describe('LoginPage: tenant branding', () => {
  it('shows the static Saarthi name/logo before branding loads, or if it fails', async () => {
    renderLogin();
    expect(await screen.findByRole('heading', { name: 'Saarthi' })).toBeInTheDocument();
    expect(getBranding).toHaveBeenCalled();
  });

  it("swaps in ELEVATE's own tenant name and logo once branding loads", async () => {
    getBranding.mockResolvedValueOnce({
      ok: true, status: 200, data: { result: { name: 'Saathi', logo: 'https://cdn.example/logo.png' } },
    });
    extractBranding.mockReturnValueOnce({
      name: 'Saathi', logoUrl: 'https://cdn.example/logo.png', allowedAuthModes: [],
    });

    renderLogin();

    expect(await screen.findByRole('heading', { name: 'Saathi' })).toBeInTheDocument();
    expect(screen.getByRole('img', { name: 'Saathi' })).toHaveAttribute('src', 'https://cdn.example/logo.png');
  });
});

describe('LoginPage: config-driven Password/OTP tabs', () => {
  it('shows both tabs by default (the static config mock)', async () => {
    renderLogin();
    await screen.findByLabelText('Phone number / email');
    expect(screen.getByRole('tab', { name: 'Password' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'OTP' })).toBeInTheDocument();
  });

  it('hides the tabs and shows only the OTP flow when the tenant allows just OTP', async () => {
    getBranding.mockResolvedValueOnce({ ok: true, status: 200, data: {} });
    extractBranding.mockReturnValueOnce({ name: '', logoUrl: '', allowedAuthModes: ['otp'] });

    renderLogin();

    // Waits on the Send OTP button, not on the identifier label: the label is
    // "Phone number / email" in BOTH modes now (either is accepted either
    // way), so it no longer distinguishes them. The button does.
    await screen.findByRole('button', { name: 'Send OTP' });
    expect(screen.queryByRole('tablist')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Password')).not.toBeInTheDocument();
    expect(screen.getByLabelText('Phone number / email')).toBeInTheDocument();
  });

  it('hides the tabs and shows only the Password flow when the tenant allows just Password', async () => {
    getBranding.mockResolvedValueOnce({ ok: true, status: 200, data: {} });
    extractBranding.mockReturnValueOnce({ name: '', logoUrl: '', allowedAuthModes: ['password'] });

    renderLogin();

    await screen.findByLabelText('Password');
    expect(screen.queryByRole('tablist')).not.toBeInTheDocument();
    expect(screen.getByLabelText('Phone number / email')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Send OTP' })).not.toBeInTheDocument();
  });

  it('keeps the tabs when the tenant explicitly allows both', async () => {
    getBranding.mockResolvedValueOnce({ ok: true, status: 200, data: {} });
    extractBranding.mockReturnValueOnce({ name: '', logoUrl: '', allowedAuthModes: ['password', 'otp'] });

    renderLogin();

    await screen.findByRole('tablist');
    expect(screen.getByRole('tab', { name: 'Password' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'OTP' })).toBeInTheDocument();
  });

  it('switches away from an already-selected mode the tenant turns out not to allow', async () => {
    // Defaults to Password (both enabled in the static mock), then branding
    // resolves to otp-only -- the form must not strand the user on a
    // Password field ELEVATE would reject.
    getBranding.mockResolvedValueOnce({ ok: true, status: 200, data: {} });
    extractBranding.mockReturnValueOnce({ name: '', logoUrl: '', allowedAuthModes: ['otp'] });

    renderLogin();

    await waitFor(() => expect(screen.queryByLabelText('Password')).not.toBeInTheDocument());
    expect(screen.getByLabelText('Phone number / email')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Send OTP' })).toBeInTheDocument();
  });
});
