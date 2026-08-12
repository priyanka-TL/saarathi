import { useState } from 'react';

import Modal from '../common/Modal.jsx';
import { useProfile } from '../../context/ProfileContext.jsx';
import { cx } from '../../utils/cx';
import ProfileForm from './ProfileForm.jsx';
import { BriefcaseIcon, GlobeIcon, InfoIcon, MapPinIcon, SchoolIcon, UserIcon } from '../icons';

/**
 * The profile dialog: view the details, or edit them.
 *
 * ONE COMPONENT FOR EVERY ENTRY POINT -- the "View Profile" menu button (opens
 * in `view`) and the post-login completion popup (opens in `edit`). Two
 * dialogs would be two sets of validation free to drift, and the difference
 * between them is a title and a starting mode.
 *
 * The modes are one dialog rather than two stacked ones: opening a second modal
 * over the first leaves two backdrops and two focus traps fighting each other.
 *
 * VIEW MODE READS AS A PROFILE CARD, NOT A FORM DUMP. Name and role are
 * promoted into a header (avatar, heading, a plain subtitle line) instead of
 * sitting in the same row shape as School/District/State -- those two are what
 * you look for first, so they get the space. Deliberately NOT a coloured
 * badge/pill: this is a business-card layout, and a bright chip next to a
 * person's title reads as a marketing flourish rather than information.
 *
 * COPY IS FORMAL THROUGHOUT: full sentences, no contractions, no dashes-as-
 * punctuation. "Missing details" uses the brand colour, never the
 * error/accent colour -- an incomplete profile is routine, not a fault, and
 * should not read like one.
 */

/** School/District/State, in `missing_fields` order -- Name and Role moved to
 * the header above, so they no longer duplicate into this list. */
const DETAIL_ROWS = [
  { name: 'school_name', label: 'School', icon: SchoolIcon },
  { name: 'district', label: 'District', icon: MapPinIcon },
  { name: 'state', label: 'State', icon: MapPinIcon },
];

const LANGUAGE_LABELS = { en: 'English', hi: 'हिंदी', kn: 'ಕನ್ನಡ', te: 'తెలుగు' };

/**
 * Up to two initials from a name, for the avatar. `null` when there is no name
 * yet, so the caller can fall back to a person icon -- an empty circle would
 * look broken rather than merely empty.
 */
function initialsOf(name) {
  if (!name) return null;
  const letters = name.trim().split(/\s+/).filter(Boolean).slice(0, 2).map((word) => word[0]);
  return letters.length ? letters.join('').toUpperCase() : null;
}

/** The line under the header: how complete the profile is, as one plain
 * sentence rather than a bare count or a warning. */
function statusLine(missingFields) {
  const count = missingFields.length;
  if (count === 0) return 'Your profile is complete and synced with your Shikshalokam account.';
  const noun = count === 1 ? 'detail' : 'details';
  const verb = count === 1 ? 'is' : 'are';
  const pronoun = count === 1 ? 'it' : 'them';
  return `${count} required ${noun} ${verb} missing. Complete ${pronoun} below to personalise your Saarthi experience.`;
}

export default function ProfileModal({ onClose, prompted = false, initialMode = 'edit' }) {
  const { profile, missingFields, loading, unavailable, save } = useProfile();
  const [mode, setMode] = useState(initialMode);

  const handleSave = async (fields) => {
    const result = await save(fields);
    if (result.ok) {
      setMode('success');
      setTimeout(() => {
        onClose();
      }, 2500);
    }
    return result;
  };

  const title = mode === 'edit'
    ? (prompted ? 'Complete your profile' : 'Update your profile')
    : 'Your profile';

  return (
    <Modal title={title} onClose={onClose}>
      {/*
        EXPLAINED, NOT HIDDEN. The menu button that opens this dialog always
        renders, so this branch is what the user sees when the deployment has
        no ELEVATE user service configured (/api/profile answers 503). Styled
        as a notice rather than a bare sentence, with its own Close action, so
        an unprompted dead end still reads as a deliberate, professional answer.
      */}
      {unavailable ? (
        <>
          <div className="profile-modal-notice">
            <InfoIcon />
            <p>
              Profile details are not available. Your organisation has not
              configured a user service for this deployment. Contact your
              administrator to set <code>ELEVATE_BASE_URL</code>.
            </p>
          </div>
          <div className="profile-form-actions">
            <button type="button" className="auth-secondary-btn" onClick={onClose}>
              Close
            </button>
          </div>
        </>
      ) : mode === 'success' ? (
        <div className="profile-modal-success">
          <div className="profile-modal-success-icon">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
              <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"></path>
              <polyline points="22 4 12 14.01 9 11.01"></polyline>
            </svg>
          </div>
          <p>Profile updated successfully!</p>
        </div>
      ) : mode === 'edit' ? (
        <>
          <p className="profile-modal-intro">
            {prompted
              ? 'Complete the following details to help Saarthi personalise its responses to your school and region.'
              : 'Update your profile details below. Saarthi uses this information to personalise its responses.'}
          </p>
          <ProfileForm
            profile={profile}
            missingFields={missingFields}
            onSave={handleSave}
            // From the view dialog, cancelling goes BACK rather than closing --
            // the user came from a read-only screen they may still want.
            onCancel={initialMode === 'view' ? () => setMode('view') : onClose}
            cancelLabel={initialMode === 'view' ? 'Cancel' : 'Not now'}
          />
        </>
      ) : (
        <>
          <p className="profile-modal-intro">
            Update your profile for a better and more personalised Saarthi experience.
          </p>

          <div className="profile-details-list">
            <div className="profile-detail-item">
              <div className="profile-detail-icon">
                <UserIcon />
              </div>
              <div className="profile-detail-content">
                <div className="profile-detail-label">Full name</div>
                <div className={cx('profile-detail-value', !profile?.name && 'profile-detail-empty')}>
                  {profile?.name || 'Not set'}
                </div>
              </div>
            </div>
            
            <div className="profile-detail-item">
              <div className="profile-detail-icon">
                <BriefcaseIcon />
              </div>
              <div className="profile-detail-content">
                <div className="profile-detail-label">Role</div>
                <div className={cx('profile-detail-value', !profile?.role && 'profile-detail-empty')}>
                  {profile?.role || 'Not set'}
                </div>
              </div>
            </div>
            {DETAIL_ROWS.map(({ name, label, icon: Icon }) => (
              <div className="profile-detail-item" key={name}>
                <div className="profile-detail-icon">
                  <Icon />
                </div>
                <div className="profile-detail-content">
                  <div className="profile-detail-label">{label}</div>
                  <div className={cx('profile-detail-value', !profile?.[name] && 'profile-detail-empty')}>
                    {profile?.[name] || 'Not set'}
                  </div>
                </div>
              </div>
            ))}
            <div className="profile-detail-item">
              <div className="profile-detail-icon">
                <GlobeIcon />
              </div>
              <div className="profile-detail-content">
                <div className="profile-detail-label">Language</div>
                <div className={cx('profile-detail-value', !profile?.preferred_language && 'profile-detail-empty')}>
                  {LANGUAGE_LABELS[profile?.preferred_language] || profile?.preferred_language || 'Not set'}
                </div>
              </div>
            </div>
          </div>

          <div className="profile-form-actions">
            <button type="button" className="auth-secondary-btn" onClick={onClose}>
              Close
            </button>
            <button
              type="button"
              className="auth-submit-btn"
              onClick={() => setMode('edit')}
              disabled={loading}
            >
              Edit profile
            </button>
          </div>
        </>
      )}
    </Modal>
  );
}
