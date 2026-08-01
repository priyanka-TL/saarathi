import { describe, expect, it } from 'vitest';

import { cx } from '../utils/cx';

describe('cx', () => {
  it('joins plain string arguments with a space', () => {
    expect(cx('a', 'b', 'c')).toBe('a b c');
  });

  it('drops falsy arguments', () => {
    expect(cx('a', false, null, undefined, '', 'b')).toBe('a b');
  });

  it('supports conditional expressions inline', () => {
    const active = true;
    const hidden = false;
    expect(cx('base', active && 'active', hidden && 'hidden')).toBe('base active');
  });

  it('returns an empty string when everything is falsy', () => {
    expect(cx(false, null, undefined)).toBe('');
  });
});
