import { render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it } from 'vitest';

import { AuthProvider } from '../context/AuthContext.jsx';
import RequireAuth from '../routes/RequireAuth.jsx';
import { STORAGE_KEYS } from '../constants';

function renderAt(path) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <AuthProvider>
        <Routes>
          <Route path="/login" element={<div>login page</div>} />
          <Route
            path="/"
            element={(
              <RequireAuth>
                <div>protected chat</div>
              </RequireAuth>
            )}
          />
        </Routes>
      </AuthProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  localStorage.clear();
});

describe('RequireAuth', () => {
  it('redirects to /login when no token is stored', () => {
    renderAt('/');
    expect(screen.getByText('login page')).toBeInTheDocument();
  });

  it('renders the protected route when a token is stored', () => {
    localStorage.setItem(STORAGE_KEYS.authToken, 'jwt-1');
    renderAt('/');
    expect(screen.getByText('protected chat')).toBeInTheDocument();
  });
});
