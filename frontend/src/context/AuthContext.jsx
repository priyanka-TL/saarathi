import { createContext, useCallback, useContext, useMemo, useState } from 'react';

import { readAuthToken, readAuthUser, writeAuth } from '../utils/storage';

/**
 * Who is logged in, and the one place that changes.
 *
 * The token comes from ELEVATE's login (src/api/elevateAuth.js), never from
 * Saarthi's own backend -- Saarthi only validates it. Persisted in
 * localStorage so a reload or a new tab stays logged in, the same choice
 * already made for the voice-language preference (see utils/storage.js).
 *
 * `http.js`'s request interceptor reads the token from storage directly
 * rather than through this context, since it is a plain module and cannot
 * use a hook -- this context is the read/write surface for React, storage is
 * the shared source of truth both sides agree on.
 */
const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const [token, setToken] = useState(() => readAuthToken());
  const [user, setUser] = useState(() => readAuthUser());

  const login = useCallback((nextToken, nextUser) => {
    writeAuth(nextToken, nextUser);
    setToken(nextToken);
    setUser(nextUser ?? null);
  }, []);

  const logout = useCallback(() => {
    writeAuth(null, null);
    setToken(null);
    setUser(null);
  }, []);

  const value = useMemo(
    () => ({ token, user, isAuthenticated: Boolean(token), login, logout }),
    [token, user, login, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth must be used inside AuthProvider');
  return ctx;
}
