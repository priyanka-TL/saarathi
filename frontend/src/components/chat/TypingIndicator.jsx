/**
 * The three bouncing dots.
 *
 * There is no streaming anywhere in this app -- replies arrive as one complete
 * block -- so this indicator IS the entire "the agent is thinking" affordance.
 * The stagger comes from CSS (:nth-child animation-delay), not from here.
 */
export default function TypingIndicator({ visible }) {
  return (
    <div id="typing-indicator" className={`typing-indicator ${visible ? '' : 'hidden'}`}>
      <span />
      <span />
      <span />
    </div>
  );
}
