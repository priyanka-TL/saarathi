import { COPY } from '../../constants';
import { formatTime } from '../../utils/time';
import { safeHttpsUrl } from '../../utils/url';
import { BotIcon, UserIcon } from '../icons';
import MessageAttachments from './MessageAttachments';
import MessageOptions from './MessageOptions';
import SpeakerButton from './SpeakerButton';

/**
 * One transcript item.
 *
 * THE XSS BOUNDARY: only 'agent' content is injected as HTML (markdown, after
 * DOMPurify). Every other kind renders as a React child, which escapes by
 * default -- the same line the original drew between innerHTML and
 * textContent, now enforced by the framework instead of by discipline.
 *
 * The four DOM shapes are reproduced exactly, including the fact that a
 * context-switch pill has NO avatar, NO .message-text wrapper and NO meta row.
 */

function Meta({ timestamp, agentName, showAgent }) {
  return (
    <div className="message-meta">
      <span className="message-time">{formatTime(timestamp)}</span>
      {showAgent ? ` · ${agentName || COPY.homeContext}` : null}
    </div>
  );
}

export default function Message({ item, onSelectOption, speech }) {
  const {
    kind, content, agentName, timestamp, options, readOnly, selectedOptionId, attachments,
  } = item;

  // Centred pill. Bare .message-content, nothing else.
  if (kind === 'context-switch') {
    return (
      <div className="message context-switch">
        <div className="message-content">{content}</div>
      </div>
    );
  }

  // Avatar sits AFTER the body, so flex order puts it on the right -- the
  // mirror of the agent row. Attribution reads 'Chat History' for a replayed
  // turn (`readOnly`, set only by useConversation's restore path) and the
  // current context name for one typed this session.
  if (kind === 'user') {
    return (
      <div className="message user">
        <div className="message-body">
          <div className="message-content">
            <div className="message-text">{content}</div>
            <Meta
              timestamp={timestamp}
              agentName={readOnly ? COPY.chatHistoryContext : agentName}
              showAgent
            />
          </div>
        </div>
        <div className="message-avatar">
          <UserIcon />
        </div>
      </div>
    );
  }

  const isAgent = kind === 'agent';

  // 'session-complete' carries a report link appended INSIDE .message-text,
  // which is what the original's _appendReportAction did -- not into
  // .message-content, which also holds the meta row.
  const reportUrl = kind === 'session-complete' ? safeHttpsUrl(item.reportUrl) : null;

  return (
    <div className={`message ${isAgent ? 'agent' : 'system'}`}>
      <div className="message-avatar">
        <BotIcon />
      </div>
      <div className="message-body">
        <div className="message-content">
          {isAgent ? (
            <div
              className="message-text"
              // Sanitized in utils/markdown.js. Agent replies are the only
              // externally-controlled content rendered as HTML anywhere.
              dangerouslySetInnerHTML={{ __html: item.html }}
            />
          ) : (
            <div className="message-text">
              {kind === 'session-finalizing' && <span className="session-spinner" />}
              {content}
              {reportUrl && (
                <a className="report-link" href={reportUrl} target="_blank" rel="noreferrer">
                  {COPY.downloadReport}
                </a>
              )}
            </div>
          )}
          <div className="message-footer">
            <Meta timestamp={timestamp} agentName={agentName} showAgent />
            {/*
              Agent replies only, and fed `content` -- the RAW MARKDOWN -- not
              `item.html`. strip_markdown_for_tts on the backend is written
              against markdown; handing it rendered HTML would make it strip
              tags it was never designed for and read table markup aloud.

              `speech` is absent in tests and any other caller that does not
              wire up the hook, so the button simply does not render there.
            */}
            {isAgent && speech && content && (
              <SpeakerButton
                isPlaying={speech.playingId === item.id}
                isLoading={speech.loadingId === item.id}
                onToggle={() => speech.toggle(item.id, content)}
              />
            )}
          </div>
        </div>
        {/*
          Outside the isAgent branch, exactly like the option group below, so a
          replayed agent bubble renders its documents too. Downloads ignore
          readOnly on purpose -- see MessageAttachments.
        */}
        <MessageAttachments attachments={attachments} />
        {options && options.length > 0 && (
          <MessageOptions
            options={options}
            readOnly={readOnly}
            selectedOptionId={selectedOptionId}
            onSelect={onSelectOption}
          />
        )}
      </div>
    </div>
  );
}
