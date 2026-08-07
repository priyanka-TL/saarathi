/**
 * Recording-side audio helpers.
 *
 * NO FORMAT CONVERSION HAPPENS HERE. The browser records whatever container it
 * prefers and uploads it as-is; the backend runs ffmpeg to produce the 16 kHz
 * mono WAV Bhashini needs. That is how Mitra does it, and it is the right split:
 * a JS resampler would have to be correct on every browser, whereas ffmpeg is
 * correct once.
 */

/**
 * Candidate recording formats, best first.
 *
 * Opus in WebM is small and universally accepted by ffmpeg. Safari supports
 * neither and needs MP4/AAC. The empty string is the last resort -- passing no
 * mimeType at all lets the browser pick, which is always better than passing
 * one it will reject.
 */
const PREFERRED_MIME_TYPES = [
  'audio/webm;codecs=opus',
  'audio/webm',
  'audio/ogg;codecs=opus',
  'audio/mp4',
  '',
];

/** Whether this browser can record at all. */
export function isRecordingSupported() {
  return (
    typeof window !== 'undefined' &&
    typeof window.MediaRecorder !== 'undefined' &&
    !!navigator.mediaDevices?.getUserMedia
  );
}

/**
 * Whether getUserMedia will be permitted here.
 *
 * The API is gated on a secure context, so over plain HTTP on a LAN address it
 * is simply absent -- and the failure arrives as an unhelpful TypeError on
 * click. Checking up front lets the button render disabled with a reason.
 * `localhost` counts as secure, which is why dev works.
 */
export function isSecureContextForMedia() {
  if (typeof window === 'undefined') return false;
  return window.isSecureContext === true;
}

/** The first candidate this browser accepts. */
export function pickMimeType() {
  if (typeof window?.MediaRecorder?.isTypeSupported !== 'function') return '';
  return PREFERRED_MIME_TYPES.find((type) => type === '' || MediaRecorder.isTypeSupported(type)) ?? '';
}

/**
 * Root-mean-square amplitude of a recording, in 0..1.
 *
 * Used to drop silent recordings before they cost an upload and an ASR call --
 * the same 0.02 gate Mitra applies. Decoding is cheap next to the round trip it
 * avoids, and a user who taps the mic twice by accident gets an instant answer
 * instead of a five-second wait for an empty transcript.
 *
 * Returns null when the clip cannot be decoded, which callers must treat as
 * "unknown, proceed" -- refusing to upload because the analysis failed would
 * turn a decoder quirk into a broken microphone.
 */
export async function computeRms(blob) {
  const AudioCtx = window.AudioContext || window.webkitAudioContext;
  if (!AudioCtx || !blob?.size) return null;

  let context;
  try {
    context = new AudioCtx();
    const buffer = await context.decodeAudioData(await blob.arrayBuffer());
    const samples = buffer.getChannelData(0);

    let sum = 0;
    for (let i = 0; i < samples.length; i += 1) sum += samples[i] * samples[i];
    return Math.sqrt(sum / samples.length);
  } catch {
    return null;
  } finally {
    // Browsers cap concurrent AudioContexts (~6 in Safari), so leaking one per
    // recording makes the sixth take silently stop working.
    context?.close?.();
  }
}

/**
 * Stop every track on a stream.
 *
 * Not optional cleanup: while a track is live the browser shows a recording
 * indicator and holds the microphone, and on mobile that drains the battery
 * until the tab is closed.
 */
export function stopStream(stream) {
  stream?.getTracks?.().forEach((track) => track.stop());
}
