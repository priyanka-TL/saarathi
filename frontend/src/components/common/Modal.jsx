import { useEffect, useId, useRef } from 'react';
import { createPortal } from 'react-dom';

import { CloseIcon } from '../icons';

/**
 * A dialog. The app's first, and bespoke on purpose -- adding a modal library
 * would be a new dependency for one screen (see CLAUDE.md).
 *
 * PORTALLED TO document.body, NOT rendered in place. The sidebar is
 * `position: fixed` with its own stacking context on mobile, and the chat
 * column scrolls -- a dialog rendered inside either would be clipped by an
 * ancestor's overflow or painted under it. `react-dom` is already a dependency,
 * so this costs nothing.
 *
 * `dismissible={false}` removes the close button and the backdrop/Escape
 * handlers, for the one case where an owner needs the choice to be explicit.
 * The profile dialog does NOT use it -- an unprompted popup that cannot be
 * dismissed traps someone who does not want to fill a form right now.
 */
export default function Modal({ title, onClose, dismissible = true, children }) {
  const titleId = useId();
  const panelRef = useRef(null);

  // Escape closes. Bound to the document rather than the panel because focus
  // may sit on the backdrop or move into a child that stops propagation.
  useEffect(() => {
    if (!dismissible) return undefined;
    const onKeyDown = (event) => {
      if (event.key === 'Escape') onClose?.();
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [dismissible, onClose]);

  /**
   * Move focus in on open, and put it back where it was on close.
   *
   * Without the restore, dismissing the dialog drops focus to the top of the
   * document, so a keyboard user is silently sent back to the start of the page.
   */
  useEffect(() => {
    const previous = document.activeElement;
    panelRef.current?.focus();
    return () => {
      // Optional-called rather than instanceof-checked: `activeElement` is
      // typed as Element (only HTMLElement has focus()), and `?.()` covers both
      // that and the null case without adding HTMLElement to eslint's
      // deliberately minimal globals list.
      previous?.focus?.();
    };
  }, []);

  return createPortal(
    <div
      className="modal-backdrop"
      // A backdrop click closes, but ONLY when the backdrop itself was the
      // target -- without that check, a click starting inside the panel and
      // ending on the backdrop (a drag over a text selection) would close the
      // dialog and discard what the user had typed.
      onClick={(event) => {
        if (dismissible && event.target === event.currentTarget) onClose?.();
      }}
    >
      <div
        className="modal-panel"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        // Focusable so the effect above has somewhere to put focus, but not a
        // tab stop -- Tab should reach the form's fields, not the container.
        tabIndex={-1}
        ref={panelRef}
      >
        <div className="modal-header">
          <h2 className="modal-title" id={titleId}>
            {title}
          </h2>
          {dismissible && (
            <button
              type="button"
              className="modal-close"
              // "Close dialog", not "Close": a dialog's own content may well
              // have a Close action of its own (the profile view does), and two
              // controls with the identical accessible name are ambiguous to a
              // screen-reader user reading a button list -- and to a test.
              aria-label="Close dialog"
              onClick={onClose}
            >
              <CloseIcon />
            </button>
          )}
        </div>

        <div className="modal-body">{children}</div>
      </div>
    </div>,
    document.body,
  );
}
