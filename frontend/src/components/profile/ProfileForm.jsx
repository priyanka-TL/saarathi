import { useState } from 'react';

import { cx } from '../../utils/cx';

/**
 * The five profile fields, as free text.
 *
 * FREE TEXT, NOT DROPDOWNS, and that is a constraint rather than a preference:
 * neither Saarthi nor the Mitra implementation this was ported from has any
 * options/lookup API for role, school, district or state. Mitra PATCHes plain
 * strings for all four, so this matches what ELEVATE already receives. Dropdowns
 * would need ELEVATE's entity APIs, which nothing in either codebase calls.
 *
 * Plain controlled inputs and hand-written validation, in LoginPage's idiom --
 * there is no form library in this app and adding one for five fields would be
 * a new dependency (CLAUDE.md).
 *
 * Reuses the `.auth-*` classes from the login page: they are the app's only
 * form styling, so a second set would be a second thing to keep in step.
 *
 * DISTRICT AND STATE SHARE A ROW. They are one idea -- "where" -- and pairing
 * them halves the form's height without touching validation: each is still its
 * own `.auth-field` with its own error, just laid out side by side.
 *
 * A LIVE PROGRESS INDICATOR sits above the fields. Five short fields with no
 * visible feedback reads as a wall of inputs; watching a bar fill in as you
 * type turns it into a checklist instead.
 */

/** Label and order. Order matters -- it matches the API's `missing_fields`. */
const FIELDS = [
  { name: 'name', label: 'Full name', autoComplete: 'name' },
  { name: 'role', label: 'Role', placeholder: 'e.g. Teacher, Head Teacher, CRP' },
  { name: 'school_name', label: 'School', placeholder: 'Your school or organisation' },
  { name: 'district', label: 'District' },
  { name: 'state', label: 'State' },
];

const [NAME_FIELD, ROLE_FIELD, SCHOOL_FIELD, DISTRICT_FIELD, STATE_FIELD] = FIELDS;

const MAX_LENGTH = 200;

export default function ProfileForm({
  profile,
  missingFields = [],
  onSave,
  onCancel,
  // "Not now" declines an unprompted popup; "Cancel" backs out of an edit the
  // user deliberately started from the view dialog. Same button, different
  // meaning, so the caller names it.
  cancelLabel = 'Not now',
}) {
  // Seeded from the current profile so a user editing one field does not have
  // to retype the other four.
  const [values, setValues] = useState(() =>
    Object.fromEntries(FIELDS.map(({ name }) => [name, profile?.[name] ?? ''])),
  );
  const [fieldErrors, setFieldErrors] = useState({});
  const [formError, setFormError] = useState('');
  const [saving, setSaving] = useState(false);

  const setValue = (name, value) => {
    setValues((current) => ({ ...current, [name]: value }));
    // Clear this field's error as soon as it is touched; leaving it up while
    // someone is fixing it reads as though the fix did not take.
    setFieldErrors((current) => (current[name] ? { ...current, [name]: '' } : current));
  };

  const handleSubmit = async (event) => {
    event.preventDefault();
    if (saving) return;

    const errors = {};
    for (const { name, label } of FIELDS) {
      const value = values[name].trim();
      if (value.length > MAX_LENGTH) errors[name] = `${label} is too long.`;
    }
    if (Object.keys(errors).length) {
      setFieldErrors(errors);
      setFormError('');
      return;
    }

    /**
     * SEND ONLY WHAT CHANGED. The API's PATCH is sparse, so trimming the body
     * to actual edits keeps the request honest -- and if every value is
     * unchanged there is nothing to save, which the backend would reject as an
     * empty update anyway.
     *
     * SPARSENESS IS NOT WHAT PROTECTS THE OTHER FIELDS -- the SERVER merges.
     * It reads the stored profile, merges these changes over it, and sends
     * ELEVATE the complete set, because ELEVATE clears whatever a write body
     * omits (see backend ProfileService.update). Sending only the diff from
     * here once wiped every untouched field. A consequence worth knowing: a
     * stale `profile` prop is harmless, since the merge baseline is upstream
     * truth rather than whatever this form last loaded.
     */
    const changed = {};
    for (const { name } of FIELDS) {
      const value = values[name].trim();
      if (value !== (profile?.[name] ?? '')) changed[name] = value;
    }
    if (!Object.keys(changed).length) {
      onCancel?.();
      return;
    }

    setSaving(true);
    setFormError('');
    const result = await onSave(changed);
    setSaving(false);
    // Left open on failure, deliberately: closing would discard what they typed
    // and leave them no way to see why it did not save.
    if (!result?.ok) setFormError(result?.error || 'Could not save your profile.');
  };

  /** One field, as its own `.auth-field` -- factored out so the district/state
   * pair below can render two of these inside a single flex row rather than
   * duplicating the markup. */
  function renderField({ name, label, placeholder, autoComplete }) {
    return (
      <div
        key={name}
        className={cx(
          'auth-field',
          // Highlights what the API said was missing, so a dialog that opened
          // by itself explains why it did.
          missingFields.includes(name) && !values[name].trim() && 'profile-field-missing',
        )}
      >
        <label htmlFor={`profile-${name}`}>{label}</label>
        <input
          id={`profile-${name}`}
          name={name}
          type="text"
          value={values[name]}
          placeholder={placeholder}
          autoComplete={autoComplete}
          maxLength={MAX_LENGTH}
          aria-invalid={fieldErrors[name] ? 'true' : undefined}
          onChange={(event) => setValue(name, event.target.value)}
        />
        {fieldErrors[name] && <div className="auth-error">{fieldErrors[name]}</div>}
      </div>
    );
  }

  const filledCount = FIELDS.filter(({ name }) => values[name].trim()).length;

  return (
    <form className="profile-form" onSubmit={handleSubmit} noValidate>
      <div
        className="profile-form-progress"
        role="progressbar"
        aria-valuenow={filledCount}
        aria-valuemin={0}
        aria-valuemax={FIELDS.length}
        aria-label="Profile completion"
      >
        <div
          className="profile-form-progress-bar"
          style={{ width: `${(filledCount / FIELDS.length) * 100}%` }}
        />
      </div>
      <p className="profile-form-progress-label">
        {filledCount} of {FIELDS.length} details completed
      </p>

      {renderField(NAME_FIELD)}
      {renderField(ROLE_FIELD)}
      {renderField(SCHOOL_FIELD)}
      <div className="profile-form-row">
        {renderField(DISTRICT_FIELD)}
        {renderField(STATE_FIELD)}
      </div>

      {formError && <div className="auth-error">{formError}</div>}

      <div className="profile-form-actions">
        {onCancel && (
          <button
            type="button"
            className="auth-secondary-btn"
            onClick={onCancel}
            disabled={saving}
          >
            {cancelLabel}
          </button>
        )}
        <button type="submit" className="auth-submit-btn" disabled={saving}>
          {saving ? 'Saving…' : 'Save profile'}
        </button>
      </div>
    </form>
  );
}
