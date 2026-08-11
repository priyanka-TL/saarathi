import { VOICE_LANGUAGES } from '../../constants';

/**
 * Which language the microphone listens in, and replies are read back in.
 *
 * VOICE ONLY. It does not change the conversation language: a Hindi recording
 * is transcribed, translated to English by the backend, and dropped into the
 * composer in English, so /api/chat still receives English and its pinned
 * contract is untouched. Full multilingual chat is separate work.
 *
 * Labels are in their own script, which is the point -- someone who needs
 * Kannada finds "ಕನ್ನಡ" faster than "Kannada". These are the same four Mitra
 * offers, and match the CHECK constraint on conversations.locale.
 *
 * Renders nothing when voice is unavailable, so a browser that cannot record
 * does not get a control that changes nothing.
 *
 * LABELLED "Preferred language", which is broader than what it does. It is
 * asked for, and reads better in the rail -- but the scope note above still
 * holds: this is the VOICE language, and picking Hindi does not make the
 * assistant reply in Hindi. If chat is ever localised, this control is where a
 * user will expect to set it.
 */
export default function LanguageSelect({ value, onChange, visible }) {
  if (!visible) return null;

  return (
    <div className="voice-language">
      {/* <label className="voice-language-label" htmlFor="voice-language-select">
        Preferred language
      </label> */}
      {/*
        A native <select>: it is four options, and the platform picker is
        better on mobile than anything worth hand-rolling here.
      */}
      <select
        id="voice-language-select"
        className="voice-language-select"
        value={value}
        onChange={(e) => onChange(e.target.value)}
      >
        {VOICE_LANGUAGES.map((language) => (
          <option key={language.value} value={language.value}>
            {language.label}
          </option>
        ))}
      </select>
    </div>
  );
}
