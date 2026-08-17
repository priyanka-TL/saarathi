import { post } from './http';
import { VOICE_SPEAK, VOICE_TRANSCRIBE, VOICE_UPLOAD_URL } from './endpoints';

/**
 * Speech-to-text and text-to-speech.
 *
 * The recording takes a three-step path and never passes through the Saarthi
 * API:
 *
 *   1. requestUploadUrl()  -- ask the backend where to put it
 *   2. uploadRecording()   -- PUT the bytes straight to the bucket
 *   3. transcribe()        -- hand back the key, get the transcript
 *
 * Step 2 is the reason this file does not use the shared axios instance for
 * everything. `src/api/http.js` pins `Content-Type: application/json` and a
 * `baseURL`, both wrong for a presigned upload to a different origin -- so the
 * upload goes through bare `fetch` instead. Mitra's client does the same.
 *
 * The server describes the upload (`method`, `headers`, `url`) rather than this
 * file assuming it, which is what lets one code path drive an S3 presigned PUT,
 * a GCS V4 signed PUT and the local dev route without branching on provider.
 */

const MAX_UPLOAD_ATTEMPTS = 3;

/**
 * Exponential backoff with jitter, capped.
 *
 * The jitter is the point: without it every client that hit a rate limit at the
 * same moment retries at the same moment. Mirrors Mitra's uploader.
 */
function backoffMs(attempt) {
  return Math.min(1000 * 2 ** attempt, 30000) + Math.random() * 1000;
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** Ask the backend to presign an upload. Returns the normalised result shape. */
export function requestUploadUrl({ conversationId, contentType }) {
  return post(VOICE_UPLOAD_URL, {
    conversation_id: conversationId,
    content_type: contentType,
  });
}

/**
 * PUT the recording to wherever the backend said, retrying on rate limits.
 *
 * Resolves to the object key on success and throws otherwise, because a failed
 * upload has nothing useful to return -- the caller shows "try again".
 */
export async function uploadRecording(presigned, blob) {
  const { uploadUrl, method = 'PUT', headers = {}, objectKey } = presigned;

  for (let attempt = 0; attempt < MAX_UPLOAD_ATTEMPTS; attempt += 1) {
    try {
      const response = await fetch(uploadUrl, {
        method,
        headers: { 'Content-Type': blob.type || 'application/octet-stream', ...headers },
        body: blob,
      });

      if (response.ok) return objectKey;

      // S3 answers 503 with a "SlowDown" body under load, and GCS uses 429.
      // Both mean "the same request will work shortly", unlike a 4xx.
      const retryable =
        response.status === 429 ||
        (response.status === 503 && (await response.text()).includes('SlowDown'));
      if (!retryable || attempt === MAX_UPLOAD_ATTEMPTS - 1) {
        throw new Error(`Upload failed: ${response.status}`);
      }
    } catch (error) {
      // A transport failure ('Failed to fetch') is worth one retry -- it is
      // usually a dropped connection rather than a rejected request. Anything
      // on the last attempt is final.
      if (attempt === MAX_UPLOAD_ATTEMPTS - 1) throw error;
    }
    await sleep(backoffMs(attempt));
  }

  throw new Error('Upload failed');
}

/** Transcribe an uploaded recording. `language` is the ASR source language. */
export function transcribe({ objectKey, language }) {
  return post(VOICE_TRANSCRIBE, { objectKey, language });
}

/** Synthesise speech for one reply. Returns `{ audio, format }` in `data`. */
export function speak({ text, language }, config) {
  return post(VOICE_SPEAK, { text, language }, config);
}
