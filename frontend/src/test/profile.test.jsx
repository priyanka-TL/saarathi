import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../api/profile', () => ({ getProfile: vi.fn(), updateProfile: vi.fn() }));
import { getProfile, updateProfile } from '../api/profile';

import ProfileGate from '../components/profile/ProfileGate.jsx';
import AccountActions from '../components/sidebar/AccountActions.jsx';
import { AuthProvider } from '../context/AuthContext.jsx';
import { ProfileProvider } from '../context/ProfileContext.jsx';
import { PROFILE_POPUP_ENABLED } from '../config/env';
import { STORAGE_KEYS } from '../constants';

/**
 * The profile feature: the "View Profile" menu row, the dialog it opens, the
 * post-login completion popup, and the flag governing whether that popup shows.
 *
 * `../api/profile` is mocked rather than axios: what these pin is the app's
 * behaviour given each API answer, and the request-building layer has its own
 * coverage in httpAuth.test.js.
 *
 * A TOKEN IS WRITTEN TO localStorage in beforeEach, deliberately. ProfileProvider
 * only fetches when authenticated -- which is what lets sidebarLayout.test.jsx
 * render the rail with no network stub -- so without one, nothing here would
 * ever call the API.
 */

const COMPLETE = {
  user_id: '1355',
  name: 'Asha Rao',
  role: 'Teacher',
  school_name: 'GHS Anekal',
  district: 'Bengaluru Urban',
  state: 'Karnataka',
  preferred_language: 'kn',
  has_accepted_tnc: true,
};

const INCOMPLETE = { ...COMPLETE, school_name: '', state: '' };

const ok = (profile, missing = []) => ({
  status: 200,
  ok: true,
  data: { profile, is_complete: missing.length === 0, missing_fields: missing },
});

function mount(ui) {
  return render(
    <AuthProvider>
      <ProfileProvider>{ui}</ProfileProvider>
    </AuthProvider>,
  );
}

/** Open the profile dialog the way a user does. */
const openDialog = () => userEvent.click(screen.getByRole('button', { name: /View Profile/ }));
const clickEdit = () => userEvent.click(screen.getByRole('button', { name: 'Edit profile' }));

beforeEach(() => {
  localStorage.setItem(STORAGE_KEYS.authToken, 'test-token');
  getProfile.mockResolvedValue(ok(COMPLETE));
  updateProfile.mockReset();
});

afterEach(() => {
  localStorage.clear();
  vi.clearAllMocks();
});

// ---------------------------------------------------------------------------
// the menu row
// ---------------------------------------------------------------------------

describe('the account actions row', () => {
  it('renders even before the profile has loaded', () => {
    // THE BUG THIS FIXES. The collapsible panel this replaced returned null
    // whenever /api/profile answered 503, so on a backend with no
    // ELEVATE_BASE_URL the profile entry silently disappeared from the menu.
    mount(<AccountActions />);

    expect(screen.getByRole('button', { name: /View Profile/ })).toBeTruthy();
  });

  it('still renders when the user service is not configured', async () => {
    getProfile.mockResolvedValue({
      status: 503, ok: false, data: { error_code: 'PROFILE_UNAVAILABLE' },
    });

    mount(<AccountActions />);
    await waitFor(() => expect(getProfile).toHaveBeenCalled());

    expect(screen.getByRole('button', { name: /View Profile/ })).toBeTruthy();
  });

  it('still renders when the profile fails to load for any other reason', async () => {
    // A chat app first: a failed profile read must not take the rail with it.
    getProfile.mockResolvedValue({ status: 502, ok: false, data: { error: 'upstream' } });

    mount(<AccountActions />);
    await waitFor(() => expect(getProfile).toHaveBeenCalled());

    expect(screen.getByRole('button', { name: /View Profile/ })).toBeTruthy();
  });

  it('sits on one row with Logout, both wearing the same class', async () => {
    // Two separate rows with two different button shapes read as unrelated
    // controls that happened to be adjacent. They are one thing.
    const { container } = mount(<AccountActions />);
    await waitFor(() => expect(getProfile).toHaveBeenCalled());

    const buttons = [...container.querySelectorAll('.account-actions button')];
    expect(buttons.map((b) => b.textContent)).toEqual(['View Profile', 'Logout']);
    buttons.forEach((b) => expect(b).toHaveClass('account-action-btn'));
  });

  it('fetches once, not once per consumer', async () => {
    // The row and the gate read the same context; two useProfile() hooks would
    // mean two GETs per load and two copies free to disagree after a save.
    mount(<><AccountActions /><ProfileGate /></>);

    await waitFor(() => expect(getProfile).toHaveBeenCalled());
    expect(getProfile).toHaveBeenCalledTimes(1);
  });
});

// ---------------------------------------------------------------------------
// the view dialog
// ---------------------------------------------------------------------------

describe('the profile dialog', () => {
  it('shows the details fetched from the user service', async () => {
    mount(<AccountActions />);
    await waitFor(() => expect(getProfile).toHaveBeenCalled());

    await openDialog();

    expect(screen.getByRole('dialog')).toBeTruthy();
    expect(screen.getByText('Your profile')).toBeTruthy();
    expect(screen.getByText('GHS Anekal')).toBeTruthy();
    expect(screen.getByText('Bengaluru Urban')).toBeTruthy();
    expect(screen.getByText('Karnataka')).toBeTruthy();
    // preferred_language 'kn' is shown in its own script, not as a code.
    expect(screen.getByText('ಕನ್ನಡ')).toBeTruthy();
  });

  it('lists every field in one uniform row shape', async () => {
    // Name and role get the same icon/label/value treatment as the rest, so
    // the dialog reads as a record rather than a mix of header styles.
    mount(<AccountActions />);
    await waitFor(() => expect(getProfile).toHaveBeenCalled());

    await openDialog();

    // document.body, not a render-returned container: the dialog is portalled.
    const rows = [...document.body.querySelectorAll('.profile-detail-item')];
    const labels = rows.map((r) => r.querySelector('.profile-detail-label').textContent);
    expect(labels).toEqual(['Full name', 'Role', 'School', 'District', 'State', 'Language']);
    expect(rows[0].querySelector('.profile-detail-value')).toHaveTextContent('Asha Rao');
    expect(rows[1].querySelector('.profile-detail-value')).toHaveTextContent('Teacher');
  });

  it('shows "Not set" rather than a blank for a field with no value', async () => {
    // A blank cell reads as a rendering fault; this reads as "not provided".
    getProfile.mockResolvedValue(ok({ ...COMPLETE, name: '', role: '' }, ['name', 'role']));
    mount(<AccountActions />);
    await waitFor(() => expect(getProfile).toHaveBeenCalled());

    await openDialog();

    expect(document.body.querySelectorAll('.profile-detail-empty')).toHaveLength(2);
    expect(screen.getAllByText('Not set')).toHaveLength(2);
  });

  it('marks exactly the unset fields, not the populated ones', async () => {
    getProfile.mockResolvedValue(ok(INCOMPLETE, ['school_name', 'state']));
    const { baseElement } = mount(<AccountActions />);
    await waitFor(() => expect(getProfile).toHaveBeenCalled());

    await openDialog();

    // baseElement, not container: the dialog is portalled to document.body.
    expect(baseElement.querySelectorAll('.profile-detail-empty').length).toBe(2);
  });

  it('explains itself with a Close action instead of showing blank fields when unconfigured', async () => {
    // The other half of the fix: the entry stays in the menu, and the reason it
    // cannot show anything is stated where the user can read it.
    getProfile.mockResolvedValue({ status: 503, ok: false, data: {} });
    mount(<AccountActions />);
    await waitFor(() => expect(getProfile).toHaveBeenCalled());

    await openDialog();

    expect(screen.getByText(/has not configured a user service/)).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Close' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Edit profile' })).toBeNull();
  });

  it('closes from either the footer action or the header X', async () => {
    mount(<AccountActions />);
    await waitFor(() => expect(getProfile).toHaveBeenCalled());

    await openDialog();
    await userEvent.click(screen.getByRole('button', { name: 'Close' }));
    expect(screen.queryByRole('dialog')).toBeNull();

    // The two must stay distinguishable by accessible name -- "Close" vs
    // "Close dialog" -- or neither a screen-reader user nor this test can tell
    // them apart.
    await openDialog();
    await userEvent.click(screen.getByRole('button', { name: 'Close dialog' }));
    expect(screen.queryByRole('dialog')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// the completion popup, and the flag
// ---------------------------------------------------------------------------

describe('the profile completion popup', () => {
  it('is enabled by default', () => {
    // The case the feature exists for, so configuring nothing gets it.
    expect(PROFILE_POPUP_ENABLED).toBe(true);
  });

  it('appears when mandatory details are missing', async () => {
    getProfile.mockResolvedValue(ok(INCOMPLETE, ['school_name', 'state']));

    mount(<ProfileGate />);

    expect(await screen.findByRole('dialog')).toBeTruthy();
    // Opens straight into the FORM, unlike the menu row's read-only view.
    expect(screen.getByText('Complete your profile')).toBeTruthy();
    expect(screen.getByLabelText('School')).toBeTruthy();
  });

  it('does not appear for a complete profile', async () => {
    mount(<ProfileGate />);

    await waitFor(() => expect(getProfile).toHaveBeenCalled());
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('does not flash while the profile is still loading', async () => {
    // `isComplete` is false before the answer arrives, so without the loading
    // guard the dialog would appear for a moment on every single load.
    let resolve;
    getProfile.mockReturnValue(new Promise((r) => { resolve = r; }));

    mount(<ProfileGate />);

    expect(screen.queryByRole('dialog')).toBeNull();
    resolve(ok(INCOMPLETE, ['state']));
    expect(await screen.findByRole('dialog')).toBeTruthy();
  });

  it('does not appear when the backend has no user service configured', async () => {
    getProfile.mockResolvedValue({ status: 503, ok: false, data: {} });

    mount(<ProfileGate />);

    await waitFor(() => expect(getProfile).toHaveBeenCalled());
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('can be dismissed, and does not immediately return', async () => {
    // A gate, not a trap: someone who does not want to fill a form now must be
    // able to get to the chat.
    getProfile.mockResolvedValue(ok(INCOMPLETE, ['school_name', 'state']));
    mount(<ProfileGate />);
    await screen.findByRole('dialog');

    await userEvent.click(screen.getByRole('button', { name: 'Not now' }));

    expect(screen.queryByRole('dialog')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// updating
// ---------------------------------------------------------------------------

describe('updating the profile', () => {
  /** Row -> view dialog -> edit mode, which is how a user reaches the form. */
  async function openForm() {
    mount(<AccountActions />);
    await waitFor(() => expect(getProfile).toHaveBeenCalled());
    await openDialog();
    await clickEdit();
  }

  it('shows a live progress indicator that updates as fields are completed', async () => {
    // Five short inputs with no feedback reads as a wall of boxes -- watching
    // this move turns filling them in into a visible checklist.
    getProfile.mockResolvedValue(ok(INCOMPLETE, ['school_name', 'state']));
    mount(<ProfileGate />);
    await screen.findByRole('dialog');

    expect(screen.getByText('3 of 5 details completed')).toBeTruthy();

    await userEvent.type(screen.getByLabelText('School'), 'GHS Anekal');

    expect(await screen.findByText('4 of 5 details completed')).toBeTruthy();
  });

  it('pairs District and State on one row', async () => {
    // One idea -- "where" -- so they share a row instead of doubling the
    // form's height with two full-width fields.
    await openForm();

    const district = screen.getByLabelText('District');
    const state = screen.getByLabelText('State');

    expect(district.closest('.profile-form-row')).not.toBeNull();
    expect(district.closest('.profile-form-row')).toBe(state.closest('.profile-form-row'));
  });

  it('is reachable from the menu row, popup or no popup', async () => {
    // What makes the flag safe to turn off: updating becomes voluntary, not
    // impossible.
    await openForm();

    expect(screen.getByText('Update your profile')).toBeTruthy();
    expect(screen.getByLabelText('Role')).toHaveValue('Teacher');
  });

  it('sends only the fields that changed', async () => {
    // Sparse PATCH: editing one field must not blank the other four.
    updateProfile.mockResolvedValue(ok({ ...COMPLETE, district: 'Mysuru' }));
    await openForm();

    const district = screen.getByLabelText('District');
    await userEvent.clear(district);
    await userEvent.type(district, 'Mysuru');
    await userEvent.click(screen.getByRole('button', { name: 'Save profile' }));

    await waitFor(() => expect(updateProfile).toHaveBeenCalledWith({ district: 'Mysuru' }));
  });

  it('confirms the save, without a second read', async () => {
    // No second GET: the backend re-reads from the user service before
    // answering, so the PATCH response IS the refreshed state.
    updateProfile.mockResolvedValue(ok({ ...COMPLETE, district: 'Mysuru' }));
    await openForm();

    const district = screen.getByLabelText('District');
    await userEvent.clear(district);
    await userEvent.type(district, 'Mysuru');
    await userEvent.click(screen.getByRole('button', { name: 'Save profile' }));

    expect(await screen.findByText('Profile updated successfully!')).toBeTruthy();
    expect(getProfile).toHaveBeenCalledTimes(1);
  });

  it('requires every mandatory field before it will send anything', async () => {
    getProfile.mockResolvedValue(ok(INCOMPLETE, ['school_name', 'state']));
    mount(<ProfileGate />);
    await screen.findByRole('dialog');

    await userEvent.click(screen.getByRole('button', { name: 'Save profile' }));

    expect(await screen.findByText('School is required.')).toBeTruthy();
    expect(screen.getByText('State is required.')).toBeTruthy();
    expect(updateProfile).not.toHaveBeenCalled();
  });

  it('does not send a blank value that would erase a good one', async () => {
    await openForm();

    await userEvent.clear(screen.getByLabelText('Role'));
    await userEvent.click(screen.getByRole('button', { name: 'Save profile' }));

    expect(await screen.findByText('Role is required.')).toBeTruthy();
    expect(updateProfile).not.toHaveBeenCalled();
  });

  it('stays open and explains itself when the save is rejected', async () => {
    // Closing would discard what they typed and leave no way to see why.
    updateProfile.mockResolvedValue({
      status: 400,
      ok: false,
      data: { error: 'userRole is not a known entity', error_code: 'PROFILE_REJECTED' },
    });
    await openForm();

    const role = screen.getByLabelText('Role');
    await userEvent.clear(role);
    await userEvent.type(role, 'Wizard');
    await userEvent.click(screen.getByRole('button', { name: 'Save profile' }));

    expect(await screen.findByText('userRole is not a known entity')).toBeTruthy();
    expect(screen.getByRole('dialog')).toBeTruthy();
    expect(role).toHaveValue('Wizard');
  });

  it('prefills the form so one edit does not mean retyping five fields', async () => {
    await openForm();

    expect(screen.getByLabelText('Full name')).toHaveValue('Asha Rao');
    expect(screen.getByLabelText('Role')).toHaveValue('Teacher');
    expect(screen.getByLabelText('School')).toHaveValue('GHS Anekal');
  });

  it('goes back to the view when the edit is cancelled', async () => {
    // Cancel from the view dialog means "back", not "close" -- the user came
    // from a read-only screen they may well still want to be looking at.
    await openForm();

    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));

    expect(screen.getByText('Your profile')).toBeTruthy();
    expect(screen.getByRole('dialog')).toBeTruthy();
  });

  it('makes no request when nothing was actually changed', async () => {
    await openForm();

    await userEvent.click(screen.getByRole('button', { name: 'Save profile' }));

    await waitFor(() => expect(screen.getByText('Your profile')).toBeTruthy());
    expect(updateProfile).not.toHaveBeenCalled();
  });
});
