import { renderHook, act, render, screen } from '@testing-library/react';
import { describe, expect, it, vi, beforeEach } from 'vitest';

import Message from '../components/chat/Message';
import { ConversationProvider } from '../context/ConversationContext.jsx';
import { useChatMessages } from '../hooks/useChatMessages';
import { useConversation } from '../hooks/useConversation';

/**
 * THE REQUIREMENT: reopening a conversation from the sidebar must still show
 * the generated documents, still downloadable.
 *
 * This drives the REAL restore path -- `useConversation.loadConversation`
 * against a real `GET /api/conversations/{id}/messages` body -- rather than
 * asserting on a hand-built item, because the failure this guards against is a
 * mapping omission in that function. Nothing else in the UI links to the
 * document, so losing it here loses it permanently.
 */

vi.mock('../api/conversations', () => ({ getMessages: vi.fn() }));
vi.mock('../api/chat', () => ({ resetConversation: vi.fn() }));

const { getMessages } = await import('../api/conversations');

const PDF = {
  file_name: 'MIP_student-focus-primary-grades',
  format: 'pdf',
  media_type: 'application/pdf',
  url: 'https://qa-mohini-static.shikshalokam.org/chatbot/2/x/1786-MIP.pdf',
};
const DOCX = { ...PDF, format: 'docx', url: 'https://qa-mohini-static.shikshalokam.org/chatbot/2/x/1786-MIP.docx' };

/** A three-turn transcript: plain reply, a turn with documents, plain reply. */
const historyBody = {
  conversation_id: 'c1',
  flow: { title: 'Saathi', stops: ['Saathi'], current_index: 0 },
  sessions: [],
  messages: [
    {
      id: 'm1', role: 'user', content: 'help with attendance',
      agent_name: null, agent_session_id: 's1', options: null,
      attachments: null, selected_option_id: null,
      created_at: '2026-08-11T09:00:00+00:00',
    },
    {
      id: 'm2', role: 'assistant', content: 'Tell me more.',
      agent_name: 'Saathi', agent_session_id: 's1', options: null,
      attachments: null, selected_option_id: null,
      created_at: '2026-08-11T09:00:01+00:00',
    },
    {
      id: 'm3', role: 'assistant', content: 'Your plan is ready to download.',
      agent_name: 'Saathi', agent_session_id: 's1', options: null,
      attachments: [PDF, DOCX], selected_option_id: null,
      created_at: '2026-08-11T09:00:02+00:00',
    },
    {
      id: 'm4', role: 'assistant', content: 'Anything else?',
      agent_name: 'Saathi', agent_session_id: 's1', options: null,
      attachments: null, selected_option_id: null,
      created_at: '2026-08-11T09:00:03+00:00',
    },
  ],
};

const wrapper = ({ children }) => <ConversationProvider>{children}</ConversationProvider>;

function restore() {
  return renderHook(
    () => {
      const messages = useChatMessages();
      const conversation = useConversation({
        messages,
        polls: { clearAll: vi.fn(), track: vi.fn(), stopOne: vi.fn() },
        onFlow: vi.fn(),
        onClearActive: vi.fn(),
        onSession: { renderCompleted: vi.fn() },
      });
      return { messages, conversation };
    },
    { wrapper },
  );
}

beforeEach(() => vi.clearAllMocks());

describe('documents survive a conversation being reopened', () => {
  it('restores attachments onto the message that produced them', async () => {
    getMessages.mockResolvedValue({ ok: true, status: 200, data: historyBody });

    const { result } = restore();
    await act(async () => {
      await result.current.conversation.loadConversationHistory('c1');
    });

    const items = result.current.messages.items;
    const withFiles = items.filter((i) => i.attachments);

    expect(withFiles).toHaveLength(1);
    expect(withFiles[0].content).toBe('Your plan is ready to download.');
    expect(withFiles[0].attachments).toEqual([PDF, DOCX]);
  });

  it('does not leak them onto neighbouring turns', async () => {
    getMessages.mockResolvedValue({ ok: true, status: 200, data: historyBody });

    const { result } = restore();
    await act(async () => {
      await result.current.conversation.loadConversationHistory('c1');
    });

    const byContent = Object.fromEntries(
      result.current.messages.items.map((i) => [i.content, i.attachments]),
    );
    expect(byContent['Tell me more.']).toBeNull();
    expect(byContent['Anything else?']).toBeNull();
  });

  it('renders the restored documents as working links', async () => {
    getMessages.mockResolvedValue({ ok: true, status: 200, data: historyBody });

    const { result } = restore();
    await act(async () => {
      await result.current.conversation.loadConversationHistory('c1');
    });

    const item = result.current.messages.items.find((i) => i.attachments);
    // readOnly is set by the restore path -- the whole point is that it does
    // not disable a download.
    expect(item.readOnly).toBe(true);

    render(<Message item={item} onSelectOption={vi.fn()} />);

    const links = screen.getAllByRole('link');
    expect(links).toHaveLength(2);
    expect(links.map((a) => a.getAttribute('href'))).toEqual([PDF.url, DOCX.url]);
  });

  it('a history with no attachments replays exactly as before', async () => {
    getMessages.mockResolvedValue({
      ok: true,
      status: 200,
      data: { ...historyBody, messages: historyBody.messages.map((m) => ({ ...m, attachments: null })) },
    });

    const { result } = restore();
    await act(async () => {
      await result.current.conversation.loadConversationHistory('c1');
    });

    expect(result.current.messages.items.every((i) => i.attachments === null)).toBe(true);
  });

  it('tolerates a server that omits the field entirely', async () => {
    // An older backend, or a message row written before the column existed.
    // eslint-disable-next-line no-unused-vars -- destructured purely to OMIT it
    const messages = historyBody.messages.map(({ attachments, ...rest }) => rest);
    getMessages.mockResolvedValue({ ok: true, status: 200, data: { ...historyBody, messages } });

    const { result } = restore();
    await act(async () => {
      await result.current.conversation.loadConversationHistory('c1');
    });

    expect(result.current.messages.items.every((i) => i.attachments === null)).toBe(true);
  });
});
