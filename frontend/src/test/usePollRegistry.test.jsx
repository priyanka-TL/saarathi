import { renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { usePollRegistry } from '../hooks/usePollRegistry';

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

describe('usePollRegistry', () => {
  it('track/stopOne/clearAll behave as before trackedInterval was added', () => {
    const { result } = renderHook(() => usePollRegistry());
    const fn = vi.fn();

    const id = result.current.track(setInterval(fn, 100));
    vi.advanceTimersByTime(250);
    expect(fn).toHaveBeenCalledTimes(2);

    result.current.stopOne(id);
    vi.advanceTimersByTime(500);
    expect(fn).toHaveBeenCalledTimes(2); // stopped, no further calls

    const fn2 = vi.fn();
    result.current.track(setInterval(fn2, 100));
    result.current.clearAll();
    vi.advanceTimersByTime(500);
    expect(fn2).not.toHaveBeenCalled();
  });

  describe('trackedInterval', () => {
    it('fires the callback on every tick', () => {
      const { result } = renderHook(() => usePollRegistry());
      const fn = vi.fn();

      result.current.trackedInterval(fn, 100);
      vi.advanceTimersByTime(350);

      expect(fn).toHaveBeenCalledTimes(3);
    });

    it('its stop() cancels only its own timer, leaving others tracked by the registry running', () => {
      const { result } = renderHook(() => usePollRegistry());
      const fnA = vi.fn();
      const fnB = vi.fn();

      const stopA = result.current.trackedInterval(fnA, 100);
      result.current.trackedInterval(fnB, 100);

      vi.advanceTimersByTime(100);
      expect(fnA).toHaveBeenCalledTimes(1);
      expect(fnB).toHaveBeenCalledTimes(1);

      stopA();
      vi.advanceTimersByTime(300);

      expect(fnA).toHaveBeenCalledTimes(1); // stopped
      expect(fnB).toHaveBeenCalledTimes(4); // still running
    });

    it('a subsequent clearAll() does not error after stop() already removed the timer', () => {
      const { result } = renderHook(() => usePollRegistry());
      const stop = result.current.trackedInterval(vi.fn(), 100);

      stop();
      expect(() => result.current.clearAll()).not.toThrow();
    });
  });
});
