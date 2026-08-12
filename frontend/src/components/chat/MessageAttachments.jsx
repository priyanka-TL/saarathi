import { COPY } from '../../constants';
import { safeHttpsUrl } from '../../utils/url';

/**
 * The downloadable documents offered alongside an agent message.
 *
 * DELIBERATELY NOT MessageOptions, though it sits in the same slot. An option
 * is click-to-reply: clicking one echoes its label as a user message and posts
 * its value as the next turn. These are plain links to a file. Putting a URL
 * through the option path would have sent it to the agent as user input.
 *
 * ONE PILL PER FILE TYPE, no filename: "Download: PDF", "Download: DOCX". The
 * reply itself already says what the document is ("Your plan is ready to
 * download"), so repeating a slug like `MIP_student-focus-primary-grades` on
 * the buttons adds noise. The name is still used -- as the `download` hint, so
 * a saved file is not called `1786427418-MIP_student-focus-primary-grades.pdf`.
 *
 * NO `readOnly` HANDLING, and that is the point of difference from options. A
 * replayed option must be inert because clicking it would fire a real turn; a
 * replayed download is a link to a document that still exists, and disabling it
 * would mean a user who reopens a conversation from the sidebar can see that a
 * plan was generated but can never get it.
 *
 * A URL that fails the https check is dropped rather than rendered dead. The
 * server already allowlists the host; this is the same defence-in-depth the
 * report link has always had.
 */
export default function MessageAttachments({ attachments }) {
  const files = (attachments || [])
    .map((file) => ({ ...file, href: safeHttpsUrl(file.url) }))
    .filter((file) => file.href);

  if (files.length === 0) return null;

  return (
    <div className="message-attachments">
      {files.map((file) => {
        const format = (file.format || '').toUpperCase();
        return (
          <a
            key={`${file.format}-${file.href}`}
            className="attachment-link"
            href={file.href}
            target="_blank"
            rel="noreferrer"
            // A HINT, not a guarantee: browsers honour `download` only for
            // same-origin URLs, and these are served from a static host. It
            // costs nothing and is correct if the asset ever moves same-origin.
            download={file.file_name ? `${file.file_name}.${file.format}` : undefined}
            data-format={file.format}
          >
            {`${COPY.downloadPrefix}${format}`}
          </a>
        );
      })}
    </div>
  );
}
