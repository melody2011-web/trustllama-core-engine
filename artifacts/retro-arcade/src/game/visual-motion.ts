export interface VisualAxis {
  position: number;
  velocity: number;
}

export interface VisualMotionOptions {
  stiffness: number;
  damping: number;
  snapDistance: number;
}

export function smoothVisualAxis(
  current: VisualAxis,
  target: number,
  deltaSeconds: number,
  options: VisualMotionOptions,
): VisualAxis {
  const dt = Math.max(0, Math.min(deltaSeconds, 1 / 20));
  const distance = target - current.position;

  if (Math.abs(distance) >= options.snapDistance) {
    return { position: target, velocity: 0 };
  }

  const acceleration = distance * options.stiffness;
  const velocity = (current.velocity + acceleration * dt)
    * Math.exp(-options.damping * dt);
  const position = current.position + velocity * dt;
  const crossedTarget = distance !== 0
    && Math.sign(target - position) !== Math.sign(distance);

  if (
    crossedTarget
    || (Math.abs(target - position) < 0.0001 && Math.abs(velocity) < 0.001)
  ) {
    return { position: target, velocity: 0 };
  }

  return { position, velocity };
}