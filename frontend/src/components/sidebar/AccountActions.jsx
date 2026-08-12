import { useState } from 'react';

import { useAuth } from '../../context/AuthContext.jsx';
import ProfileModal from '../profile/ProfileModal.jsx';
import { LogoutIcon, UserIcon } from '../icons';

/**
 * The account block at the end of the rail: View Profile and Logout, side by
 * side.
 *
 * ONE COMPONENT, ONE ROW, TWO IDENTICAL BUTTONS. These were briefly two
 * separate rows with two different button shapes -- a full-width rounded
 * rectangle above a small pill -- which read as two unrelated bits of chrome
 * that happened to be adjacent. They are one thing: what you can do with your
 * account. Same row, same class, equal width.
 *
 * Logout stays SECOND, so the destructive action is still the last control in
 * the rail.
 *
 * The signed-in name is deliberately NOT here. Two buttons already fill the
 * row, and the name has a better home: it is the first field in the dialog this
 * opens. The rail previously showed a raw identifier in this spot, which is the
 * problem the profile work set out to fix.
 */
export default function AccountActions() {
  const { logout } = useAuth();
  const [viewing, setViewing] = useState(false);

  return (
    <div className="account-actions">
      <button
        type="button"
        className="account-action-btn"
        onClick={() => setViewing(true)}
      >
        <UserIcon />
        View Profile
      </button>

      <button type="button" className="account-action-btn" onClick={logout}>
        <LogoutIcon />
        Logout
      </button>

      {/* Portalled to document.body, so it is outside this flex row and cannot
          affect the two buttons' layout. */}
      {viewing && <ProfileModal initialMode="view" onClose={() => setViewing(false)} />}
    </div>
  );
}
