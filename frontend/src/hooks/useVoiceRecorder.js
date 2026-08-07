import { useCallback, useEffect, useRef, useState } from 'react';

import { requestUploadUrl, transcribe, uploadRecording } from '../api/voice';
import { COPY, VOICE_MAX_RECORDING_MS, VOICE_SILENCE_RMS } from '../constants';
import {
  computeRms,
  isRecordingSupported,
  isSecureContextForMedia,
  pickMimeType,
  stopStream,
} from '../utils/audio';

/**
 * Record a message, transcribe it, and hand back the text.
 *
 * THE TRANSCRIPT IS NEVER SENT AUTOMATICALLY. It lands in the composer for the
 * user to read and correct, exactly as Mitra does. ASR on a t4-class model over
 * accented or code-mixed speech is good, not right, and an editable box is the
 * difference between a wrong word and a wrong message.
 *
 * The `voiceDisabled` flag is how a 503 VOICE_DISABLED from the backend
 * switches the feature off in the UI: one failed attempt is enough to know the
 * deployment has VOICE_ENABLED=0, and the button hides rather than inviting a
 * second click that cannot work.
 *
 * STATE MACHINE: idle -> recording -> transcribing -> idle. `stop()` is a no-op
 * outside `recording`, so a double click cannot land in a state with no exit.
 */
export function useVoiceRecorder({ conversationId, language, onTranscript }) {
  const [isRecording, setIsRecording] = useState(false);
  const [isTranscribing, setIsTranscribing] = useState(false);
  const [error, setError] = useState(null);
  const [voiceDisabled, setVoiceDisabled] = useState(false);

  const recorderRef = useRef(null);
  const streamRef = useRef(null);
  const chunksRef = useRef([]);
  const timeoutRef = useRef(null);
  // Set when the component unmounts mid-recording, so the async onstop handler
  // knows not to touch state that no longer exists.
  const abandonedRef = useRef(false);

  const cleanup = useCallback(() => {
    clearTimeout(timeoutRef.current);
    timeoutRef.current = null;
    stopStream(streamRef.current);
    streamRef.current = null;
    recorderRef.current = null;
    chunksRef.current = [];
  }, []);

  // Releasing the microphone on unmount is not optional: a live track keeps the
  // browser's recording indicator lit and, on mobile, drains the battery until
  // the tab is closed.
  useEffect(() => () => {
    abandonedRef.current = true;
    cleanup();
  }, [cleanup]);

  const handleRecordingStopped = useCallback(async () => {
    const chunks = chunksRef.current;
    const mimeType = recorderRef.current?.mimeType || '';
    cleanup();
    setIsRecording(false);

    if (abandonedRef.current) return;
    if (!chunks.length) {
      setError(COPY.micSilent);
      return;
    }

    const blob = new Blob(chunks, { type: mimeType || 'audio/webm' });

    // Cheap local check before spending an upload and an ASR call. `null` means
    // the clip could not be decoded, which is NOT evidence of silence -- a
    // decoder quirk must not look like a broken microphone.
    const rms = await computeRms(blob);
    if (rms !== null && rms < VOICE_SILENCE_RMS) {
      setError(COPY.micSilent);
      return;
    }

    setIsTranscribing(true);
    setError(null);
    try {
      const presign = await requestUploadUrl({
        conversationId,
        contentType: blob.type,
      });
      if (presign.status === 503) {
        setVoiceDisabled(true);
        return;
      }
      if (!presign.ok) throw new Error(presign.data?.error_code || 'UPLOAD_URL_FAILED');

      const objectKey = await uploadRecording(presign.data, blob);

      const result = await transcribe({ objectKey, language });
      if (result.status === 503) {
        setVoiceDisabled(true);
        return;
      }
      if (!result.ok) {
        // 413 is the one failure with a specific remedy the user can act on.
        setError(
          result.data?.error_code === 'AUDIO_TOO_LARGE' ? COPY.micTooLong : COPY.micFailed,
        );
        return;
      }

      const transcript = (result.data?.transcript || '').trim();
      if (!transcript) {
        setError(COPY.micSilent);
        return;
      }
      onTranscript(transcript);
    } catch {
      setError(COPY.micFailed);
    } finally {
      if (!abandonedRef.current) setIsTranscribing(false);
    }
  }, [cleanup, conversationId, language, onTranscript]);

  const start = useCallback(async () => {
    setError(null);

    if (!isSecureContextForMedia()) {
      setError(COPY.micInsecure);
      return;
    }
    if (!isRecordingSupported()) {
      setError(COPY.micUnsupported);
      return;
    }

    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (err) {
      // These three are the ones a user can actually do something about, and
      // they need different advice, so they are not collapsed.
      if (err?.name === 'NotAllowedError' || err?.name === 'SecurityError') {
        setError(COPY.micDenied);
      } else if (err?.name === 'NotFoundError' || err?.name === 'DevicesNotFoundError') {
        setError(COPY.micNoDevice);
      } else {
        setError(COPY.micUnsupported);
      }
      return;
    }

    const mimeType = pickMimeType();
    let recorder;
    try {
      // An empty mimeType means "browser's choice" and must be omitted rather
      // than passed as '' -- Safari rejects the empty string.
      recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
    } catch {
      stopStream(stream);
      setError(COPY.micUnsupported);
      return;
    }

    streamRef.current = stream;
    recorderRef.current = recorder;
    chunksRef.current = [];

    recorder.ondataavailable = (event) => {
      if (event.data?.size) chunksRef.current.push(event.data);
    };
    recorder.onstop = handleRecordingStopped;
    recorder.onerror = () => {
      cleanup();
      setIsRecording(false);
      setError(COPY.micFailed);
    };

    recorder.start();
    setIsRecording(true);

    // Backstop for a user who starts recording and walks away. Without it the
    // upload eventually fails on the backend's size limit, which is a worse
    // way to find out.
    timeoutRef.current = setTimeout(() => {
      if (recorderRef.current?.state === 'recording') recorderRef.current.stop();
    }, VOICE_MAX_RECORDING_MS);
  }, [cleanup, handleRecordingStopped]);

  const stop = useCallback(() => {
    if (recorderRef.current?.state === 'recording') recorderRef.current.stop();
  }, []);

  const toggle = useCallback(() => {
    if (isRecording) stop();
    else start();
  }, [isRecording, start, stop]);

  return {
    isRecording,
    isTranscribing,
    error,
    clearError: useCallback(() => setError(null), []),
    voiceDisabled,
    // The feature is pointless without somewhere to attribute the recording to,
    // and the backend requires a conversation it can check ownership against.
    supported: isRecordingSupported() && isSecureContextForMedia() && !!conversationId,
    start,
    stop,
    toggle,
  };
}
