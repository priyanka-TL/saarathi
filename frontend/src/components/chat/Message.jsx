import { COPY } from '../../constants';
import { formatTime } from '../../utils/time';
import { safeReportUrl } from '../../utils/url';
import { BotIcon } from '../icons';
import MessageOptions from './MessageOptions';

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

export default function Message({ item, onSelectOption }) {
  const { kind, content, agentName, timestamp, options, readOnly, selectedOptionId } = item;

  // Centred pill. Bare .message-content, nothing else.
  if (kind === 'context-switch') {
    return (
      <div className="message context-switch">
        <div className="message-content">{content}</div>
      </div>
    );
  }

  // No avatar, meta shows the time only.
  if (kind === 'user') {
    return (
      <div className="message user">
        <div className="message-body">
          <div className="message-content">
            <div className="message-text">{content}</div>
            <Meta timestamp={timestamp} showAgent={false} />
          </div>
        </div>
      </div>
    );
  }

  const isAgent = kind === 'agent';

  // 'session-complete' carries a report link appended INSIDE .message-text,
  // which is what the original's _appendReportAction did -- not into
  // .message-content, which also holds the meta row.
  const reportUrl = kind === 'session-complete' ? safeReportUrl(item.reportUrl) : null;

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
                <a className="report-link" href={reportUrl} target="_blank" rel="noopener">
                  {COPY.downloadReport}
                </a>
              )}
            </div>
          )}
          <Meta timestamp={timestamp} agentName={agentName} showAgent />
        </div>
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
