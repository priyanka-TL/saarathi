import { useEffect, useState } from 'react';

import { PROFILE_POPUP_ENABLED, PROFILE_POPUP_REPROMPT_HOURS } from '../../config/env';
import { useProfile } from '../../context/ProfileContext.jsx';
import ProfileModal from './ProfileModal.jsx';

const SNOOZE_KEY = 'saarthi_profile_snoozed_until';

/**
 * Pops the profile form up after login when mandatory fields are missing.
 *
 * FOUR CONDITIONS, ALL REQUIRED, and each rules out a different wrong popup:
 *
 *   PROFILE_POPUP_ENABLED  the operator wants it (default on). Off leaves the
 *                          Profile section fully usable -- see config/env.js.
 *   !loading               don't flash the dialog before the answer arrives:
 *                          `isComplete` is false while loading, so without this
 *                          every load would show it for a moment.
 *   !unavailable           the backend has no ELEVATE user service (503), so
 *                          there is nothing to save to.
 *   !isComplete            the server's verdict, never recomputed here.
 *
 * A GATE, NOT A TRAP. Dismissing it is remembered for the session, so someone
 * who declines is not asked again on every re-render or route change -- but the
 * next visit asks once more, since the profile really is still incomplete. The
 * chat itself is never blocked.
 */
export default function ProfileGate() {
  const { isComplete, loading, unavailable } = useProfile();
  const [dismissed, setDismissed] = useState(() => {
    const snoozedUntil = localStorage.getItem(SNOOZE_KEY);
    if (snoozedUntil && Date.now() < parseInt(snoozedUntil, 10)) {
      return true;
    }
    return false;
  });

  const handleDismiss = () => {
    setDismissed(true);
    const snoozeTime = Date.now() + PROFILE_POPUP_REPROMPT_HOURS * 60 * 60 * 1000;
    localStorage.setItem(SNOOZE_KEY, snoozeTime.toString());
  };

  // A user who completes their profile and later empties a field should be
  // asked again; resetting on `isComplete` going true keeps the dismissal from
  // outliving the thing it was dismissing.
  useEffect(() => {
    if (isComplete) {
      setDismissed(false);
      localStorage.removeItem(SNOOZE_KEY);
    }
  }, [isComplete]);

  if (!PROFILE_POPUP_ENABLED) return null;
  if (loading || unavailable || isComplete || dismissed) return null;

  return <ProfileModal prompted onClose={handleDismiss} />;
}
