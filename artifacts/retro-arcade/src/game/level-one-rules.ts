import type { LevelOneRun } from '@/hooks/use-arcade-engine';

export interface LevelOnePosition {
  x: number;
  y: number;
}

export interface LevelOneStep {
  position: LevelOnePosition;
  run: LevelOneRun;
}

function segmentIntersectsExpandedBox(
  current: LevelOnePosition,
  requested: LevelOnePosition,
  left: number,
  right: number,
  top: number,
  bottom: number,
) {
  const inside = (position: LevelOnePosition) =>
    position.x >= left
    && position.x <= right
    && position.y >= top
    && position.y <= bottom;
  if (inside(current) && !inside(requested)) return false;

  const dx = requested.x - current.x;
  const dy = requested.y - current.y;
  let entry = 0;
  let exit = 1;

  for (const [origin, delta, minimum, maximum] of [
    [current.x, dx, left, right],
    [current.y, dy, top, bottom],
  ] as const) {
    if (Math.abs(delta) < Number.EPSILON) {
      if (origin < minimum || origin > maximum) return false;
      continue;
    }
    const first = (minimum - origin) / delta;
    const second = (maximum - origin) / delta;
    entry = Math.max(entry, Math.min(first, second));
    exit = Math.min(exit, Math.max(first, second));
    if (entry > exit) return false;
  }

  return exit >= 0 && entry <= 1;
}

export function applyLevelOneStep(
  run: LevelOneRun,
  current: LevelOnePosition,
  requested: LevelOnePosition,
): LevelOneStep {
  let nextX = requested.x;
  let nextY = requested.y;
  const hitHazard = run.hazards.some((hazard) =>
    segmentIntersectsExpandedBox(
      current,
      requested,
      hazard.x - 0.018,
      hazard.x + hazard.width + 0.018,
      hazard.y - 0.04,
      hazard.y + hazard.height + 0.04,
    )
  );

  if (hitHazard) {
    nextX = Math.max(0.03, current.x - 0.075);
    nextY = 0.5;
  }

  const collectedIds = new Set(run.collectedIds);
  run.items.forEach((item) => {
    if (Math.hypot(item.x - nextX, item.y - nextY) <= 0.06) {
      collectedIds.add(item.id);
    }
  });

  const completed = run.completed
    || (collectedIds.size === run.items.length && nextX >= run.finishX);

  return {
    position: { x: nextX, y: nextY },
    run: {
      ...run,
      collectedIds: [...collectedIds],
      completed,
    },
  };
}