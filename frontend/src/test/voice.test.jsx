import { act, renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { useSpeech } from '../hooks/useSpeech';
import { useVoiceRecorder } from '../hooks/useVoiceRecorder';
import { COPY } from '../constants';

// The three network boundaries these hooks have.
vi.mock('../api/voice', () => ({
  requestUploadUrl: vi.fn(),
  uploadRecording: vi.fn(),
  transcribe: vi.fn(),
  speak: vi.fn(),
}));
import { requestUploadUrl, speak, transcribe, uploadRecording } from '../api/voice';

// ---------------------------------------------------------------------------
// browser fakes
//
// jsdom implements none of MediaRecorder, getUserMedia or AudioContext, so
// each is stubbed to the minimum surface the hooks touch.
// ---------------------------------------------------------------------------

let recorderInstances = [];

class FakeMediaRecorder {
  static isTypeSupported(type) {
    return type === 'audio/webm;codecs=opus';
  }

  constructor(stream, options) {
    this.stream = stream;
    this.mimeType = options?.mimeType || 'audio/webm';
    this.state = 'inactive';
    recorderInstances.push(this);
  }

  start() {
    this.state = 'recording';
  }

  stop() {
    this.state = 'inactive';
    this.ondataavailable?.({ data: { size: 1024 } });
    this.onstop?.();
  }
}

// jsdom's Blob has no arrayBuffer(), which every browser back to Safari 14 and
// Chrome 76 does. computeRms treats its absence as "loudness unknown, proceed"
// -- correct behaviour, but it would make the silence tests below vacuous, so
// the method is supplied here. The value does not matter: the fake
// decodeAudioData ignores its argument.
if (!Blob.prototype.arrayBuffer) {
  Blob.prototype.arrayBuffer = function arrayBuffer() {
    return Promise.resolve(new ArrayBuffer(8));
  };
}

function stubMedia({ rms = 0.5, secure = true } = {}) {
  const track = { stop: vi.fn() };
  window.isSecureContext = secure;
  window.MediaRecorder = FakeMediaRecorder;
  navigator.mediaDevices = {
    getUserMedia: vi.fn().mockResolvedValue({ getTracks: () => [track] }),
  };
  window.AudioContext = class {
    decodeAudioData() {
      // rms of a constant signal is the constant itself.
      return Promise.resolve({ getChannelData: () => new Float32Array(16).fill(rms) });
    }
    close() {}
  };
  return track;
}

const ok = (data) => ({ status: 200, ok: true, data });
const fail = (status, error_code) => ({ status, ok: false, data: { error_code } });

beforeEach(() => {
  vi.clearAllMocks();
  recorderInstances = [];
  window.isSecureContext = true;
});

// ---------------------------------------------------------------------------
// useVoiceRecorder
// ---------------------------------------------------------------------------

function renderRecorder(overrides = {}) {
  const onTranscript = vi.fn();
  const view = renderHook(() =>
    useVoiceRecorder({
      conversationId: 'conv-1',
      language: 'en',
      onTranscript,
      ...overrides,
    }),
  );
  return { ...view, onTranscript };
}

/** Record and stop, driving the fake recorder. */
async function record(result) {
  await act(async () => {
    await result.current.start();
  });
  await act(async () => {
    recorderInstances.at(-1).stop();
  });
}

describe('useVoiceRecorder', () => {
  it('uploads, transcribes, and hands the text back WITHOUT sending it', async () => {
    stubMedia();
    requestUploadUrl.mockResolvedValue(
      ok({ uploadUrl: 'https://bucket/x', objectKey: 'voice/conv-1/a.webm' }),
    );
    uploadRecording.mockResolvedValue('voice/conv-1/a.webm');
    transcribe.mockResolvedValue(ok({ transcript: 'hello there', language: 'en' }));

    const { result, onTranscript } = renderRecorder();
    await record(result);

    await waitFor(() => expect(onTranscript).toHaveBeenCalledWith('hello there'));
    // The transcript goes to the composer. Nothing here posts a turn -- ASR is
    // good, not right, and the user must get to correct it.
    expect(transcribe).toHaveBeenCalledWith({
      objectKey: 'voice/conv-1/a.webm',
      language: 'en',
    });
  });

  it('drops a silent recording without spending an upload or an ASR call', async () => {
    stubMedia({ rms: 0.001 });

    const { result, onTranscript } = renderRecorder();
    await record(result);

    await waitFor(() => expect(result.current.error).toBe(COPY.micSilent));
    expect(requestUploadUrl).not.toHaveBeenCalled();
    expect(onTranscript).not.toHaveBeenCalled();
  });

  it('still uploads when the clip cannot be decoded', async () => {
    // A decoder quirk must not look like a broken microphone: unknown loudness
    // means proceed, not discard.
    stubMedia();
    window.AudioContext = class {
      decodeAudioData() {
        return Promise.reject(new Error('unsupported'));
      }
      close() {}
    };
    requestUploadUrl.mockResolvedValue(ok({ uploadUrl: 'u', objectKey: 'k' }));
    uploadRecording.mockResolvedValue('k');
    transcribe.mockResolvedValue(ok({ transcript: 'heard it', language: 'en' }));

    const { result, onTranscript } = renderRecorder();
    await record(result);

    await waitFor(() => expect(onTranscript).toHaveBeenCalledWith('heard it'));
  });

  it('switches the feature off when the backend answers 503', async () => {
    stubMedia();
    requestUploadUrl.mockResolvedValue(fail(503, 'VOICE_DISABLED'));

    const { result } = renderRecorder();
    await record(result);

    // One failed attempt is enough to know VOICE_ENABLED=0; the button hides
    // rather than inviting a click that cannot work.
    await waitFor(() => expect(result.current.voiceDisabled).toBe(true));
    expect(result.current.error).toBeNull();
  });

  it('names the specific remedy for an over-long recording', async () => {
    stubMedia();
    requestUploadUrl.mockResolvedValue(ok({ uploadUrl: 'u', objectKey: 'k' }));
    uploadRecording.mockResolvedValue('k');
    transcribe.mockResolvedValue(fail(413, 'AUDIO_TOO_LARGE'));

    const { result } = renderRecorder();
    await record(result);

    await waitFor(() => expect(result.current.error).toBe(COPY.micTooLong));
  });

  it('distinguishes a denied permission from an absent microphone', async () => {
    stubMedia();
    navigator.mediaDevices.getUserMedia = vi
      .fn()
      .mockRejectedValue(Object.assign(new Error('no'), { name: 'NotAllowedError' }));

    const { result } = renderRecorder();
    await act(async () => {
      await result.current.start();
    });
    expect(result.current.error).toBe(COPY.micDenied);

    navigator.mediaDevices.getUserMedia = vi
      .fn()
      .mockRejectedValue(Object.assign(new Error('no'), { name: 'NotFoundError' }));
    await act(async () => {
      await result.current.start();
    });
    expect(result.current.error).toBe(COPY.micNoDevice);
  });

  it('refuses to record on an insecure origin', async () => {
    stubMedia({ secure: false });

    const { result } = renderRecorder();
    await act(async () => {
      await result.current.start();
    });

    expect(result.current.error).toBe(COPY.micInsecure);
    expect(result.current.supported).toBe(false);
    expect(navigator.mediaDevices.getUserMedia).not.toHaveBeenCalled();
  });

  it('releases the microphone when recording stops', async () => {
    // A live track keeps the browser's recording indicator lit and drains the
    // battery on mobile until the tab is closed.
    const track = stubMedia();
    requestUploadUrl.mockResolvedValue(ok({ uploadUrl: 'u', objectKey: 'k' }));
    uploadRecording.mockResolvedValue('k');
    transcribe.mockResolvedValue(ok({ transcript: 'x' }));

    const { result } = renderRecorder();
    await record(result);

    await waitFor(() => expect(track.stop).toHaveBeenCalled());
  });

  it('is unsupported without a conversation to attribute the recording to', () => {
    stubMedia();
    const { result } = renderRecorder({ conversationId: null });
    expect(result.current.supported).toBe(false);
  });

  it('treats an empty transcript as nothing heard', async () => {
    stubMedia();
    requestUploadUrl.mockResolvedValue(ok({ uploadUrl: 'u', objectKey: 'k' }));
    uploadRecording.mockResolvedValue('k');
    transcribe.mockResolvedValue(ok({ transcript: '   ' }));

    const { result, onTranscript } = renderRecorder();
    await record(result);

    await waitFor(() => expect(result.current.error).toBe(COPY.micSilent));
    expect(onTranscript).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// useSpeech
// ---------------------------------------------------------------------------

describe('useSpeech', () => {
  let played;

  beforeEach(() => {
    played = [];
    window.Audio = class {
      constructor(src) {
        this.src = src;
        played.push(this);
        this.pause = vi.fn();
        this.currentTime = 0;
      }
      play() {
        return Promise.resolve();
      }
    };
  });

  it('synthesises a reply and plays it as a data URL', async () => {
    speak.mockResolvedValue(ok({ audio: 'QUJD', format: 'wav' }));

    const { result } = renderHook(() => useSpeech({ language: 'hi' }));
    await act(async () => {
      await result.current.toggle('msg-1', '**Hello**');
    });

    expect(speak).toHaveBeenCalledWith({ text: '**Hello**', language: 'hi' });
    expect(played.at(-1).src).toBe('data:audio/wav;base64,QUJD');
    expect(result.current.playingId).toBe('msg-1');
  });

  it('sends the RAW MARKDOWN, not rendered HTML', async () => {
    // strip_markdown_for_tts on the backend is written against markdown;
    // handing it HTML would make it read table markup aloud.
    speak.mockResolvedValue(ok({ audio: 'QUJD', format: 'wav' }));

    const { result } = renderHook(() => useSpeech({ language: 'en' }));
    await act(async () => {
      await result.current.toggle('m', '| a | b |\n**bold**');
    });

    expect(speak.mock.calls[0][0].text).toBe('| a | b |\n**bold**');
  });

  it('caches audio so re-reading a reply costs no second call', async () => {
    speak.mockResolvedValue(ok({ audio: 'QUJD', format: 'wav' }));

    const { result } = renderHook(() => useSpeech({ language: 'en' }));
    await act(async () => {
      await result.current.toggle('msg-1', 'text');
    });
    await act(async () => {
      result.current.stop();
    });
    await act(async () => {
      await result.current.toggle('msg-1', 'text');
    });

    expect(speak).toHaveBeenCalledTimes(1);
    expect(played).toHaveLength(2);
  });

  it('stops the current clip before starting another', async () => {
    // Two voices talking over each other is never what anyone wanted.
    speak.mockResolvedValue(ok({ audio: 'QUJD', format: 'wav' }));

    const { result } = renderHook(() => useSpeech({ language: 'en' }));
    await act(async () => {
      await result.current.toggle('msg-1', 'first');
    });
    const first = played.at(-1);
    await act(async () => {
      await result.current.toggle('msg-2', 'second');
    });

    expect(first.pause).toHaveBeenCalled();
    expect(result.current.playingId).toBe('msg-2');
  });

  it('toggling the playing message stops it', async () => {
    speak.mockResolvedValue(ok({ audio: 'QUJD', format: 'wav' }));

    const { result } = renderHook(() => useSpeech({ language: 'en' }));
    await act(async () => {
      await result.current.toggle('msg-1', 'text');
    });
    await act(async () => {
      await result.current.toggle('msg-1', 'text');
    });

    expect(result.current.playingId).toBeNull();
  });

  it('reports a failure and stays idle', async () => {
    speak.mockResolvedValue(fail(502, 'TTS_FAILED'));

    const { result } = renderHook(() => useSpeech({ language: 'en' }));
    await act(async () => {
      await result.current.toggle('msg-1', 'text');
    });

    expect(result.current.error).toBe('speak');
    expect(result.current.playingId).toBeNull();
    expect(result.current.loadingId).toBeNull();
  });

  it('stays silent when voice is disabled server-side', async () => {
    speak.mockResolvedValue(fail(503, 'VOICE_DISABLED'));

    const { result } = renderHook(() => useSpeech({ language: 'en' }));
    await act(async () => {
      await result.current.toggle('msg-1', 'text');
    });

    // Not an error the user did anything to cause, so nothing is shown.
    expect(result.current.error).toBeNull();
    expect(played).toHaveLength(0);
  });
});
