import { describe, expect, it } from 'vitest';
import { smoothVisualAxis } from './visual-motion';

const options = {
  stiffness: 190,
  damping: 16,
  snapDistance: 0.24,
};

describe('server snapshot presentation smoothing', () => {
  it('approaches a target without overshooting it', () => {
    let axis = { position: 0.2, velocity: 0 };

    for (let frame = 0; frame < 120; frame += 1) {
      axis = smoothVisualAxis(axis, 0.4, 1 / 60, options);
      expect(axis.position).toBeGreaterThanOrEqual(0.2);
      expect(axis.position).toBeLessThanOrEqual(0.4);
    }

    expect(axis.position).toBeCloseTo(0.4, 3);
  });

  it('caps long frame gaps and stays finite', () => {
    const axis = smoothVisualAxis(
      { position: 0.5, velocity: 0.4 },
      0.7,
      2,
      options,
    );

    expect(Number.isFinite(axis.position)).toBe(true);
    expect(Number.isFinite(axis.velocity)).toBe(true);
    expect(axis.position).toBeLessThanOrEqual(0.7);
  });

  it('snaps across authoritative teleports instead of sliding through terrain', () => {
    expect(
      smoothVisualAxis(
        { position: 0.1, velocity: 1 },
        0.9,
        1 / 60,
        options,
      ),
    ).toEqual({ position: 0.9, velocity: 0 });
  });
});