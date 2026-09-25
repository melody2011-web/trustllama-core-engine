import { describe, expect, it } from 'vitest';
import { drawWorld } from '@/components/playfield';
import type { ArcadeLevel, ArcadePlayer, ArcadeState, LevelOneRun } from '@/hooks/use-arcade-engine';

type RenderedLabel = {
  text: string;
  x: number;
  width: number;
  align: CanvasTextAlign;
};

const levels: ArcadeLevel[] = [
  { number: 2, name: 'Electric Woodlands', min_score: 101, max_score: 250, next_score: 251 },
  { number: 6, name: 'Llama Labyrinth', min_score: 801, max_score: 1200, next_score: 1201 },
  { number: 8, name: 'Llama Submarine Deep-Sea Trench', min_score: 1601, max_score: 2000, next_score: 2001 },
  { number: 10, name: 'Trust Llama Citadel Core', min_score: 2401, max_score: null, next_score: 3000 },
];

function renderLabels(
  currentLevel: number,
  width: number,
  options: { cameraX?: number; score?: number; victory?: boolean } = {},
) {
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
    moveTo: noop,
    lineTo: noop,
    stroke: noop,
    fillText(text: string, x: number, _y: number, maxWidth?: number) {
      labels.push({
        text,
        x,
        width: Math.min(text.length * 6.6, maxWidth ?? Number.POSITIVE_INFINITY),
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
  const levelOne: LevelOneRun = {
    items: [],
    hazards: [],
    collectedIds: [],
    finishX: 0.94,
    completed: false,
  };
  const state: ArcadeState = {
    players: [],
    collectibles: {},
    levels,
    level7_worlds: {},
    level8_worlds: {},
    level9_worlds: {},
    level10_worlds: {},
    starting_lives: 3,
    maximum_lives: 5,
    extra_life_catalog: [],
    simulated_entry_fee_tlama: 25,
    levelOne,
    logs: [],
  };
  const player = {
    player_id: 'player-1',
    name: 'PLAYER',
    x: 0.5,
    y: 0.2,
    score: options.score ?? levels.find(({ number }) => number === currentLevel)?.min_score ?? 0,
    current_level: currentLevel,
    victory_announced: options.victory ?? false,
  } as ArcadePlayer;

  drawWorld(
    context,
    { width, height: 600 },
    state,
    player,
    { x: 0.5, y: 0.2 },
    options.cameraX ?? 900,
    0,
    1,
  );
  return labels;
}

function expectInsideCanvas(labels: RenderedLabel[], text: string, canvasWidth: number) {
  const label = labels.find((candidate) => candidate.text === text);
  expect(label, `Expected "${text}" to be rendered`).toBeDefined();
  if (!label) return;
  const left = label.align === 'center' ? label.x - label.width / 2 : label.x;
  const right = label.align === 'center' ? label.x + label.width / 2 : label.x + label.width;
  expect(left).toBeGreaterThanOrEqual(0);
  expect(right).toBeLessThanOrEqual(canvasWidth);
}

describe('Later-level canvas labels', () => {
  it.each([320, 375])('keeps representative later-level HUD labels visible at %ipx', (width) => {
    for (const level of levels) {
      const labels = renderLabels(level.number, width);
      if (level.number === 2) {
        expectInsideCanvas(labels, 'LEVEL 2  •  SYNTH-GRID ASCENT', width);
        expectInsideCanvas(labels, 'CLIMB  •  DODGE  •  JUMP', width);
        continue;
      }
      expectInsideCanvas(labels, `LEVEL ${level.number}  •  ${level.name.toUpperCase()}`, width);
      expectInsideCanvas(labels, `SCORE ${level.min_score}/${level.next_score}  •  ADVANCE`, width);
    }
  });

  it.each([320, 375])('keeps the final completion label visible at %ipx', (width) => {
    const labels = renderLabels(10, width, { score: 3000, victory: true });
    expectInsideCanvas(labels, 'CITADEL CLEAR  •  GAME COMPLETE', width);
  });

  it.each([
    { label: 'ORBS AHEAD', cameraX: 460 },
    { label: 'CHECKPOINT', cameraX: 1950 },
  ])('keeps the $label objective sign visible near a canvas edge', ({ label, cameraX }) => {
    const width = 320;
    expectInsideCanvas(renderLabels(8, width, { cameraX }), label, width);
  });
});