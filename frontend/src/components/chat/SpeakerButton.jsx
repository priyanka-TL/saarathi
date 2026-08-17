import { SpeakerIcon, SpeakerOffIcon, SpinnerIcon } from '../icons';
import { COPY } from '../../constants';

/**
 * Read-this-out, on one agent reply.
 *
 * Per message rather than a global autoplay toggle, for three reasons: no
 * surprise audio, no TTS call for replies nobody wanted spoken, and iOS Safari
 * only permits playback started inside a user gesture -- which a click is and
 * an arriving message is not.
 */
export default function SpeakerButton({ isPlaying, isLoading, onToggle }) {
  const label = isPlaying ? COPY.speakStop : COPY.speakStart;

  return (
    <button
      type="button"
      className={`speaker-btn${isPlaying ? ' is-playing' : ''}`}
      onClick={onToggle}
      disabled={isLoading}
      aria-label={label}
      title={label}
      aria-pressed={isPlaying}
    >
      {isLoading ? <SpinnerIcon /> : isPlaying ? <SpeakerOffIcon /> : <SpeakerIcon />}
    </button>
  );
}
