import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../api/elevateAuth', () => ({
  sendRegistrationOtp: vi.fn(),
  register: vi.fn(),
  extractMessage: vi.fn((data) => data?.message ?? ''),
}));

import { register, sendRegistrationOtp } from '../api/elevateAuth';
import RegisterPage from '../pages/RegisterPage.jsx';

function renderRegister() {
  return render(
    <MemoryRouter initialEntries={['/register']}>
      <RegisterPage />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe('RegisterPage', () => {
  it('sends an OTP, then creates the account and shows success', async () => {
    sendRegistrationOtp.mockResolvedValue({ ok: true, status: 200, data: {} });
    register.mockResolvedValue({ ok: true, status: 200, data: {} });
    const user = userEvent.setup();
    renderRegister();

    await user.type(screen.getByLabelText('Name'), 'Asha');
    await user.type(screen.getByLabelText('Phone number'), '9876543210');
    await user.click(screen.getByRole('button', { name: 'Send OTP' }));

    expect(sendRegistrationOtp).toHaveBeenCalledWith({ phone: '9876543210', phone_code: '+91' });

    await user.type(await screen.findByLabelText('OTP'), '654321');
    await user.click(screen.getByRole('button', { name: 'Create account' }));

    expect(await screen.findByText(/Account created/)).toBeInTheDocument();
    expect(register).toHaveBeenCalledWith({
      phone: '9876543210', phone_code: '+91', otp: '654321', name: 'Asha', password: undefined,
    });
  });

  it('shows ELEVATE\'s own error message when the OTP send fails', async () => {
    sendRegistrationOtp.mockResolvedValue({ ok: false, status: 400, data: { message: 'Phone already registered' } });
    const user = userEvent.setup();
    renderRegister();

    await user.type(screen.getByLabelText('Phone number'), '9876543210');
    await user.click(screen.getByRole('button', { name: 'Send OTP' }));

    expect(await screen.findByText('Phone already registered')).toBeInTheDocument();
    expect(screen.queryByLabelText('OTP')).not.toBeInTheDocument();
  });
});
