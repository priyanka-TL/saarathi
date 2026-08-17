import { useCallback, useEffect, useRef, useState } from 'react';

import { speak } from '../api/voice';

/**
 * Read agent replies aloud.
 *
 * ONE AUDIO ELEMENT FOR THE WHOLE APP, held in a module-level ref. Playing a
 * second reply stops the first, because two voices talking over each other is
 * never what anyone wanted -- Mitra keeps a single `audioRef` for the same
 * reason. A per-message element would also mean N of them accumulating in a
 * long transcript.
 *
 * SYNTHESISED AUDIO IS CACHED PER MESSAGE. Re-reading a reply is a common
 * thing to do, and a TTS call costs seconds and an upstream request. The cache
 * lives as long as the page, keyed by message id.
 *
 * PLAYBACK ONLY EVER STARTS FROM A CLICK. iOS Safari blocks audio that is not
 * inside a user gesture, and the per-message button satisfies that natively --
 * which is a large part of why there is no autoplay mode.
 */
export function useSpeech({ language }) {
  const [playingId, setPlayingId] = useState(null);
  const [loadingId, setLoadingId] = useState(null);
  const [error, setError] = useState(null);

  const audioRef = useRef(null);
  const cacheRef = useRef(new Map());
  // Bumped on every request so a slow response for a message the user has
  // already moved on from cannot start playing over the current one.
  const requestRef = useRef(0);

  const stop = useCallback(() => {
    const audio = audioRef.current;
    if (audio) {
      audio.pause();
      audio.currentTime = 0;
    }
    audioRef.current = null;
    setPlayingId(null);
  }, []);

  // Leaving the page mid-sentence must not leave a voice playing.
  useEffect(() => () => {
    audioRef.current?.pause();
    audioRef.current = null;
  }, []);

  const play = useCallback(
    (id, dataUrl) => {
      audioRef.current?.pause();

      const audio = new Audio(dataUrl);
      audioRef.current = audio;
      audio.onended = () => {
        // Only clear if this clip is still the current one -- it may have been
        // superseded while it was finishing.
        if (audioRef.current === audio) {
          audioRef.current = null;
          setPlayingId(null);
        }
      };
      audio.onerror = () => {
        if (audioRef.current === audio) {
          audioRef.current = null;
          setPlayingId(null);
        }
      };

      setPlayingId(id);
      audio.play().catch(() => {
        // Autoplay policy, or a codec the browser will not take. Either way
        // the button should not stay stuck in the playing state.
        if (audioRef.current === audio) {
          audioRef.current = null;
          setPlayingId(null);
        }
      });
    },
    [],
  );

  const toggle = useCallback(
    async (id, text) => {
      if (playingId === id) {
        stop();
        return;
      }
      if (loadingId) return;

      const cached = cacheRef.current.get(id);
      if (cached) {
        play(id, cached);
        return;
      }

      const requestId = requestRef.current + 1;
      requestRef.current = requestId;

      setLoadingId(id);
      setError(null);
      try {
        const result = await speak({ text, language });

        // The user clicked something else, or stopped, while this was in
        // flight. Dropping it is the whole reason for the counter.
        if (requestRef.current !== requestId) return;

        if (!result.ok || !result.data?.audio) {
          setError(result.status === 503 ? null : 'speak');
          return;
        }

        // The container is whatever Bhashini returned -- 'wav' from
        // Bhashini/IITM/TTS. Browsers sniff the actual bytes and ignore a
        // mismatched media type here, but declaring the real one is free.
        const dataUrl = `data:audio/${result.data.format || 'wav'};base64,${result.data.audio}`;
        cacheRef.current.set(id, dataUrl);
        play(id, dataUrl);
      } catch {
        if (requestRef.current === requestId) setError('speak');
      } finally {
        if (requestRef.current === requestId) setLoadingId(null);
      }
    },
    [language, loadingId, play, playingId, stop],
  );

  return {
    playingId,
    loadingId,
    error,
    clearError: useCallback(() => setError(null), []),
    toggle,
    stop,
  };
}
