import { describe, expect, it } from 'vitest';
import { drawWorld } from '@/components/playfield';
import type {
  ArcadePlayer,
  ArcadeState,
  LevelOneRun,
} from '@/hooks/use-arcade-engine';
import { applyLevelOneStep } from './level-one-rules';

function createRun(): LevelOneRun {
  return {
    items: [
      { id: 'chip-1', x: 0.1, y: 0.2 },
      { id: 'chip-2', x: 0.25, y: 0.2 },
      { id: 'chip-3', x: 0.4, y: 0.2 },
      { id: 'chip-4', x: 0.55, y: 0.2 },
      { id: 'chip-5', x: 0.7, y: 0.2 },
    ],
    hazards: [{ id: 'laser', x: 0.3, y: 0.5, width: 0.04, height: 0.2 }],
    collectedIds: [],
    finishX: 0.94,
    completed: false,
  };
}

type RenderedLabel = {
  text: string;
  x: number;
  width: number;
  align: CanvasTextAlign;
};

function renderLevelOneLabels(run: LevelOneRun, width = 800, cameraX = 1800) {
  const labels: RenderedLabel[] = [];
  const noop = () => undefined;
  const context = {
    save: noop,
    restore: noop,
    translate: noop,
    scale: noop,
    fillRect: noop,
    strokeRect: noop,
    beginPath: noop,
    arc: noop,
    fill: noop,
    moveTo: noop,
    lineTo: noop,
    stroke: noop,
    fillText(text: string, x: number) {
      labels.push({
        text,
        x,
        width: text.length * 6.6,
        align: this.textAlign,
      });
    },
    measureText: (text: string) => ({ width: text.length * 6.6 }),
    fillStyle: '',
    strokeStyle: '',
    lineWidth: 1,
    font: '',
    textAlign: 'start',
  } as unknown as CanvasRenderingContext2D;
  const state = {
    players: [],
    collectibles: {},
    levelOne: run,
  } as unknown as ArcadeState;
  const player = {
    player_id: 'player-1',
    current_level: 1,
  } as ArcadePlayer;

  drawWorld(
    context,
    { width, height: 600 },
    state,
    player,
    { x: 0.94, y: 0.2 },
    cameraX,
    0,
    0,
    1,
  );

  return labels;
}

function labelTexts(labels: RenderedLabel[]) {
  return labels.map(({ text }) => text);
}

function expectLabelInsideCanvas(labels: RenderedLabel[], text: string, canvasWidth: number) {
  const label = labels.find((candidate) => candidate.text === text);
  expect(label, `Expected "${text}" to be rendered`).toBeDefined();
  if (!label) return;

  const left = label.align === 'center'
    ? label.x - label.width / 2
    : label.align === 'right' || label.align === 'end'
      ? label.x - label.width
      : label.x;
  const right = label.align === 'center'
    ? label.x + label.width / 2
    : label.align === 'right' || label.align === 'end'
      ? label.x
      : label.x + label.width;
  expect(left).toBeGreaterThanOrEqual(0);
  expect(right).toBeLessThanOrEqual(canvasWidth);
}

describe('Level 1 opening-stage rules', () => {
  it('collects all five TLAMA tokens', () => {
    let run = createRun();
    let position = { x: 0.03, y: 0.2 };

    for (const item of run.items) {
      const step = applyLevelOneStep(run, position, item);
      run = step.run;
      position = step.position;
    }

    expect(run.collectedIds).toEqual([
      'chip-1',
      'chip-2',
      'chip-3',
      'chip-4',
      'chip-5',
    ]);
  });

  it('pushes the player away from a hazard instead of allowing passage', () => {
    const step = applyLevelOneStep(
      createRun(),
      { x: 0.29, y: 0.6 },
      { x: 0.31, y: 0.6 },
    );

    expect(step.position.x).toBeCloseTo(0.215);
    expect(step.position.y).toBe(0.5);
    expect(step.position.x).toBeLessThan(0.3);
  });

  it('blocks a fast movement segment that crosses an entire hazard', () => {
    const step = applyLevelOneStep(
      createRun(),
      { x: 0.2, y: 0.6 },
      { x: 0.5, y: 0.6 },
    );

    expect(step.position.x).toBeCloseTo(0.125);
    expect(step.position.y).toBe(0.5);
  });

  it('allows movement parallel to a hazard without terrain sticking', () => {
    const step = applyLevelOneStep(
      createRun(),
      { x: 0.27, y: 0.2 },
      { x: 0.27, y: 0.4 },
    );

    expect(step.position).toEqual({ x: 0.27, y: 0.4 });
  });

  it('lets a server-corrected player move out of an expanded collision box', () => {
    const step = applyLevelOneStep(
      createRun(),
      { x: 0.3, y: 0.5 },
      { x: 0.25, y: 0.5 },
    );

    expect(step.position).toEqual({ x: 0.25, y: 0.5 });
  });

  it('keeps the finish gate locked until every chip is collected', () => {
    const run = createRun();
    const step = applyLevelOneStep(run, { x: 0.9, y: 0.2 }, { x: 0.95, y: 0.2 });

    expect(step.run.completed).toBe(false);
  });

  it('completes Level 1 after all chips are collected and the finish is reached', () => {
    const run = createRun();
    run.collectedIds = run.items.map((item) => item.id);
    const step = applyLevelOneStep(run, { x: 0.9, y: 0.2 }, { x: 0.95, y: 0.2 });

    expect(step.run.completed).toBe(true);
  });

  it('renders the current chip count and an offscreen exit direction before all chips are collected', () => {
    const run = createRun();
    run.collectedIds = ['chip-1', 'chip-2'];

    const labels = labelTexts(renderLevelOneLabels(run, 800, 1200));

    expect(labels).toContain('TLAMA TOKENS COLLECTED 2/5');
    expect(labels).toContain('EXIT >>');
    expect(labels).not.toContain('EXIT OPEN');
  });

  it('renders EXIT OPEN after all chips are collected', () => {
    const run = createRun();
    run.collectedIds = run.items.map((item) => item.id);

    const labels = labelTexts(renderLevelOneLabels(run, 800, 1200));

    expect(labels).toContain('EXIT OPEN');
    expect(labels).toContain('EXIT >>');
    expect(labels).toContain('TLAMA TOKENS COLLECTED 5/5');
  });

  it('keeps the exit open while authoritative Level 2 advancement arrives', () => {
    const run = createRun();
    run.collectedIds = run.items.map((item) => item.id);
    run.completed = true;

    const labels = labelTexts(renderLevelOneLabels(run, 800, 1200));

    expect(labels).toContain('EXIT OPEN');
    expect(labels).toContain('EXIT >>');
    expect(labels).toContain('TLAMA TOKENS COLLECTED 5/5');
  });

  it.each([320, 375])('keeps TLAMA token progress visible at %ipx', (width) => {
    const run = createRun();
    run.collectedIds = ['chip-1', 'chip-2'];
    const labels = renderLevelOneLabels(run, width);

    expectLabelInsideCanvas(labels, 'TLAMA TOKENS COLLECTED 2/5', width);
    expectLabelInsideCanvas(labels, 'EXIT >>', width);
  });

  it.each([320, 375])('keeps compact EXIT OPEN visible at %ipx', (width) => {
    const run = createRun();
    run.collectedIds = run.items.map((item) => item.id);
    run.completed = true;
    const labels = renderLevelOneLabels(run, width);

    expectLabelInsideCanvas(labels, 'EXIT OPEN', width);
    expectLabelInsideCanvas(labels, 'EXIT >>', width);
  });

  it.each([
    { edge: 'left', cameraX: 2280 },
    { edge: 'right', cameraX: 2200 },
  ])('keeps the finish-gate label visible at the $edge edge of a mobile canvas', ({ cameraX }) => {
    const width = 320;
    const run = createRun();
    const lockedLabels = renderLevelOneLabels(run, width, cameraX);
    expectLabelInsideCanvas(lockedLabels, 'LOCKED', width);
    expectLabelInsideCanvas(lockedLabels, 'TLAMA TOKENS COLLECTED 0/5', width);

    run.collectedIds = run.items.map((item) => item.id);
    const unlockedLabels = renderLevelOneLabels(run, width, cameraX);
    expectLabelInsideCanvas(unlockedLabels, 'EXIT OPEN', width);
  });
});
