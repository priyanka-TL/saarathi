import { cx } from '../../utils/cx';

/**
 * The option-button group under an agent message (Mitra remote_flow turns).
 *
 * Two states, matching the original exactly:
 *
 *   live      - clickable; the click echoes the LABEL as a user message and
 *               sends the VALUE.
 *   readOnly  - historical replay. Every button disabled and marked
 *               .option-btn--used, with .option-btn--selected on the one the
 *               user actually chose. No handler is attached AT ALL, so a
 *               replayed group can never re-fire a turn.
 *
 * `readOnly` is set on every group in the pane the moment any turn starts (see
 * useChatMessages' DISABLE_ALL_OPTIONS), which is what stops a double-click
 * during a slow request.
 */
export default function MessageOptions({ options, readOnly, selectedOptionId, onSelect }) {
  return (
    <div className="message-options">
      {options.map((opt) => {
        return (
          <button
            key={opt.id}
            type="button"
            className={cx(
              'option-btn',
              readOnly && 'option-btn--used',
              readOnly && opt.id === selectedOptionId && 'option-btn--selected',
            )}
            data-id={opt.id}
            data-value={opt.value}
            disabled={readOnly}
            onClick={readOnly ? undefined : () => onSelect(opt)}
          >
            {opt.label}
          </button>
        );
      })}
    </div>
  );
}
