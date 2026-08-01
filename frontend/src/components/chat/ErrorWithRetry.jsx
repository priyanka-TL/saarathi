import { useState } from 'react';

import { COPY } from '../../constants';
import { BotIcon } from '../icons';

/**
 * An error bubble with a recovery button.
 *
 * THE LABEL BRANCH IS LOAD-BEARING, not cosmetic:
 *
 *   no remote session  -> "Retry"           -> re-send the same text
 *   remote session live -> "Check for reply" -> POST /resume, read-only
 *
 * During a Mitra interview Mitra may already have answered and moved on. Blindly
 * re-sending the text then lands it against the NEXT question, and because
 * Mitra merges consecutive messages from the same sender, the original answer
 * is destroyed silently -- no error anywhere. /resume asks what actually
 * happened instead, and only its `can_resend` outcome makes a re-send safe.
 */
export default function ErrorWithRetry({ item, hasRemoteSession, onRetry, onResume }) {
  const [label, setLabel] = useState(hasRemoteSession ? COPY.checkForReply : COPY.retry);
  const [busy, setBusy] = useState(false);

  async function handleClick() {
    if (busy) return;
    setBusy(true);
    if (hasRemoteSession) {
      setLabel(COPY.checking);
      const settled = await onResume(item);
      // On a non-terminal outcome the original relabelled to "Check again"
      // rather than back to "Check for reply".
      if (!settled) {
        setLabel(COPY.checkAgain);
        setBusy(false);
      }
    } else {
      await onRetry(item);
    }
  }

  return (
    <div className="message system">
      <div className="message-avatar">
        <BotIcon />
      </div>
      <div className="message-body">
        <div className="message-content error-content">{item.content}</div>
        <button type="button" className="retry-btn" onClick={handleClick} disabled={busy}>
          {label}
        </button>
      </div>
    </div>
  );
}
