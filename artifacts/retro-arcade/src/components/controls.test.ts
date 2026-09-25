import { describe, expect, it } from 'vitest';
import { keyboardAxis, normalizedAxis } from './controls';

describe('arcade movement controls', () => {
  it('preserves arrow and WASD keyboard movement', () => {
    expect(keyboardAxis(new Set(['ArrowRight', 'ArrowUp']))).toEqual({ x: 1, y: -1 });
    expect(keyboardAxis(new Set(['KeyA', 'KeyS']))).toEqual({ x: -1, y: 1 });
    expect(keyboardAxis(new Set(['ArrowLeft', 'KeyD']))).toEqual({ x: 0, y: 0 });
  });

  it('normalizes compact mobile joystick movement in CSS pixels', () => {
    const compactPad = {
      getBoundingClientRect: () => ({
        left: 20,
        top: 40,
        width: 96,
        height: 96,
      }),
    } as HTMLElement;

    expect(normalizedAxis(92, 88, compactPad)).toEqual({ x: 0.5, y: 0 });
    expect(normalizedAxis(68, 40, compactPad)).toEqual({ x: 0, y: -1 });
  });
});