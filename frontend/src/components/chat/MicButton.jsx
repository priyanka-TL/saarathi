import { MicIcon, SpinnerIcon, StopIcon } from '../icons';
import { COPY } from '../../constants';

/**
 * The composer's record button.
 *
 * Three states, one button: press to record, press again to stop, then a
 * spinner while the transcript comes back. Keeping it one control means the
 * stop target is exactly where the finger already is.
 *
 * It renders NOTHING when voice is unavailable -- an unsupported browser, an
 * insecure origin, or a backend with VOICE_ENABLED=0. A disabled-looking button
 * that can never work is worse than no button, and the reasons a user CAN fix
 * (a denied permission, a silent take) surface as an error message instead.
 */
export default function MicButton({
  isRecording,
  isTranscribing,
  disabled,
  onToggle,
}) {
  const busy = isTranscribing;
  const label = isRecording
    ? COPY.micStop
    : busy
      ? COPY.micTranscribing
      : COPY.micStart;

  return (
    <button
      type="button"
      id="mic-btn"
      className={`mic-btn${isRecording ? ' is-recording' : ''}`}
      onClick={onToggle}
      // Never disabled while recording: the user must always be able to stop.
      disabled={busy || (disabled && !isRecording)}
      // The icon is the only visible content at every width, so the button is
      // unlabelled to a screen reader without this.
      aria-label={label}
      title={label}
      aria-pressed={isRecording}
    >
      {busy ? <SpinnerIcon /> : isRecording ? <StopIcon /> : <MicIcon />}
    </button>
  );
}
