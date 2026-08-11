import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

import Message from '../components/chat/Message';
import MessageAttachments from '../components/chat/MessageAttachments';
import { makeItem } from '../hooks/useChatMessages';

/**
 * Downloadable documents offered by a remote turn.
 *
 * The behaviour worth pinning is not "a link renders" -- it is the two places
 * this differs from the option group it sits beside:
 *
 *   1. a replayed (readOnly) download stays CLICKABLE, where a replayed option
 *      is inert. Reopening a conversation from the sidebar must not strand a
 *      generated document.
 *   2. a URL that fails the https check is DROPPED, not rendered dead.
 */

const PDF = {
  file_name: 'MIP_student-focus-primary-grades',
  format: 'pdf',
  media_type: 'application/pdf',
  url: 'https://qa-mohini-static.shikshalokam.org/chatbot/2/x/1786-MIP.pdf',
};
const DOCX = {
  file_name: 'MIP_student-focus-primary-grades',
  format: 'docx',
  media_type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  url: 'https://qa-mohini-static.shikshalokam.org/chatbot/2/x/1786-MIP.docx',
};

const links = () => screen.queryAllByRole('link');

describe('MessageAttachments', () => {
  it('renders one pill per available format', () => {
    render(<MessageAttachments attachments={[PDF, DOCX]} />);

    const rendered = links();
    expect(rendered).toHaveLength(2);
    expect(rendered.map((a) => a.textContent)).toEqual(['⬇ PDF', '⬇ DOCX']);
    expect(rendered[0]).toHaveAttribute('href', PDF.url);
    expect(rendered[1]).toHaveAttribute('href', DOCX.url);
  });

  it('renders a single pill when only one format was generated', () => {
    render(<MessageAttachments attachments={[PDF]} />);

    expect(links()).toHaveLength(1);
    expect(links()[0].textContent).toBe('⬇ PDF');
  });

  it('renders nothing at all when there are no attachments', () => {
    const { container } = render(<MessageAttachments attachments={[]} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('renders nothing when the field is absent', () => {
    const { container } = render(<MessageAttachments attachments={null} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('drops a non-https URL rather than rendering a dead link', () => {
    render(
      <MessageAttachments
        attachments={[{ ...PDF, url: 'http://insecure.example/x.pdf' }, DOCX]}
      />,
    );

    const rendered = links();
    expect(rendered).toHaveLength(1);
    expect(rendered[0].textContent).toBe('⬇ DOCX');
  });

  it('shows no filename in the label, but uses it as the download hint', () => {
    render(<MessageAttachments attachments={[PDF]} />);

    const link = links()[0];
    expect(link.textContent).not.toContain('MIP_student-focus-primary-grades');
    // Extension appended, so the saved file is not extensionless.
    expect(link).toHaveAttribute('download', 'MIP_student-focus-primary-grades.pdf');
  });

  it('omits the download hint when the platform sent no file name', () => {
    render(<MessageAttachments attachments={[{ ...PDF, file_name: '' }]} />);
    expect(links()[0]).not.toHaveAttribute('download');
  });
});

describe('attachments on a message', () => {
  it('renders under a live agent bubble', () => {
    const item = makeItem('agent', {
      content: 'Your plan is ready to download.',
      html: '<p>Your plan is ready to download.</p>',
      agentName: 'Saathi',
      attachments: [PDF, DOCX],
    });

    render(<Message item={item} onSelectOption={vi.fn()} />);
    expect(links()).toHaveLength(2);
  });

  it('STAYS CLICKABLE on a replayed message, unlike a replayed option', () => {
    // readOnly is what useConversation sets on every item restored from
    // history. An option group goes inert; a download must not, or a user who
    // reopens the conversation can see a plan was made and never get it.
    const item = makeItem('agent', {
      content: 'Your plan is ready to download.',
      html: '<p>Your plan is ready to download.</p>',
      agentName: 'Saathi',
      readOnly: true,
      attachments: [PDF, DOCX],
      options: [{ id: '1', label: 'Yes', value: 'yes' }],
    });

    render(<Message item={item} onSelectOption={vi.fn()} />);

    const rendered = links();
    expect(rendered).toHaveLength(2);
    rendered.forEach((a) => {
      expect(a).toHaveAttribute('href');
      expect(a).not.toHaveAttribute('disabled');
      expect(a.getAttribute('aria-disabled')).toBeNull();
    });

    // ...while the option beside it IS inert, which is the contrast.
    expect(screen.getByRole('button', { name: 'Yes' })).toBeDisabled();
  });

  it('leaves an ordinary message untouched', () => {
    const item = makeItem('agent', {
      content: 'Hello there.',
      html: '<p>Hello there.</p>',
      agentName: 'Saathi',
    });

    render(<Message item={item} onSelectOption={vi.fn()} />);
    expect(links()).toHaveLength(0);
  });
});
