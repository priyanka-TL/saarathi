import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';

import { getProfile, updateProfile } from '../api/profile';
import { useAuth } from './AuthContext.jsx';

/**
 * The logged-in user's profile, fetched once and shared.
 *
 * TWO CONSUMERS, ONE FETCH. The sidebar's Profile section and the completion
 * dialog both need the same answer, so a context rather than a hook called
 * twice -- two `useProfile()` calls would mean two GETs on every load and two
 * copies free to disagree after a save.
 *
 * COMPLETENESS IS THE SERVER'S ANSWER, NOT RECOMPUTED HERE. `is_complete` and
 * `missing_fields` come straight from the API, which is what keeps the
 * mandatory-field rule in one place (backend/app/services/profile_service.py).
 * Re-deriving it in JS would drift the first time a sixth field is added, and
 * the dialog would then disagree with the API about whether to show.
 */
const ProfileContext = createContext(null);

const EMPTY = { profile: null, isComplete: false, missingFields: [] };

/** The one place the API's snake_case body becomes this app's camelCase state. */
function fromResponse(data) {
  return {
    profile: data?.profile ?? null,
    isComplete: Boolean(data?.is_complete),
    missingFields: Array.isArray(data?.missing_fields) ? data.missing_fields : [],
  };
}

export function ProfileProvider({ children }) {
  const { token, isAuthenticated } = useAuth();
  const [state, setState] = useState(EMPTY);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  // Distinct from `error`: the deployment has no ELEVATE user service, so the
  // Profile section hides entirely rather than showing a failure the user can
  // do nothing about. Mirrors how voice reads its own 503.
  const [unavailable, setUnavailable] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const { status, ok, data } = await getProfile();
      if (status === 503) {
        setUnavailable(true);
        setState(EMPTY);
        return;
      }
      setUnavailable(false);
      if (!ok) {
        // A 401 needs no handling here: http.js's response interceptor has
        // already cleared the session, and RequireAuth redirects on re-render.
        setError(data?.error || 'Could not load your profile.');
        return;
      }
      setState(fromResponse(data));
    } catch {
      // Transport-level failure (server down, DNS, CORS). Not fatal -- the app
      // is a chat app first, and a missing Profile section is not worth a crash.
      setError('Could not load your profile.');
    } finally {
      setLoading(false);
    }
  }, []);

  /**
   * KEYED ON `token`, NOT ON `isAuthenticated`. A second login in the same tab
   * leaves isAuthenticated true throughout, so keying on it would keep showing
   * the PREVIOUS user's name and school to whoever logged in next.
   *
   * The guard is also what keeps this off /login: unauthenticated means no
   * fetch, which is why the login page makes no profile call and why rendering
   * a bare Sidebar in a test needs no network stub.
   */
  useEffect(() => {
    if (!isAuthenticated) {
      setState(EMPTY);
      setError('');
      setUnavailable(false);
      return;
    }
    refresh();
  }, [token, isAuthenticated, refresh]);

  /**
   * Save, and adopt the refreshed profile the PATCH already returned.
   *
   * No second GET: the backend re-reads from ELEVATE before answering, so this
   * response IS the refreshed state. Returns `{ ok, error }` so the form can
   * keep the dialog open and show the message on failure.
   */
  const save = useCallback(async (fields) => {
    setError('');
    try {
      const { status, ok, data } = await updateProfile(fields);
      if (status === 503) {
        setUnavailable(true);
        return { ok: false, error: 'Profile updates are not available.' };
      }
      if (!ok) {
        return { ok: false, error: data?.error || 'Could not save your profile.' };
      }
      setState(fromResponse(data));
      return { ok: true, error: '' };
    } catch {
      return { ok: false, error: 'Could not save your profile.' };
    }
  }, []);

  const value = useMemo(
    () => ({ ...state, loading, error, unavailable, refresh, save }),
    [state, loading, error, unavailable, refresh, save],
  );

  return <ProfileContext.Provider value={value}>{children}</ProfileContext.Provider>;
}

export function useProfile() {
  const ctx = useContext(ProfileContext);
  if (!ctx) throw new Error('useProfile must be used inside ProfileProvider');
  return ctx;
}
