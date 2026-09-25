import { useEffect, useRef, useState } from 'react';
import {
  type ArcadeCollectible,
  type ArcadePlayer,
  type ArcadeState,
  useArcadeEngine,
} from '@/hooks/use-arcade-engine';
import { smoothVisualAxis, type VisualAxis } from '@/game/visual-motion';

type Dimensions = { width: number; height: number };

const WORLD_WIDTH = 2400;
const LEVEL_TWO_WORLD_WIDTH = 1200;
const LEVEL_TWO_WORLD_HEIGHT = 2200;
const PLAYER_WIDTH = 34;
const PLAYER_HEIGHT = 30;
const HUD_MARGIN = 12;

function clamp(value: number, minimum: number, maximum: number) {
  return Math.max(minimum, Math.min(maximum, value));
}

function drawBoundedCenteredLabel(
  context: CanvasRenderingContext2D,
  text: string,
  x: number,
  y: number,
  canvasWidth: number,
) {
  const halfWidth = context.measureText(text).width / 2;
  context.fillText(text, clamp(x, halfWidth + 4, canvasWidth - halfWidth - 4), y);
}

function drawPixelLlama(
  context: CanvasRenderingContext2D,
  x: number,
  y: number,
  self: boolean,
  facing: number,
  time: number,
  moving: boolean,
  scaleMultiplier: number = 1.0,
  lockToGrid: boolean = false,
) {
  const bob = lockToGrid ? 0 : moving ? Math.sin(time / 80) * 3 : Math.sin(time / 200) * 1.5;
  const chew = moving && (Math.floor(time / 100) % 2 === 0);
  const renderedX = lockToGrid ? Math.round(x / 4) * 4 : Math.round(x);
  const renderedY = lockToGrid ? Math.round(y / 4) * 4 : Math.round(y + bob);

  context.save();
  context.translate(renderedX, renderedY);
  context.scale(facing, 1);

  const primary = self ? '#ffe45c' : '#c0c0c0';
  const furAccent = self ? '#ff2ca8' : '#ef2da8';
  const skin = self ? '#ffffff' : '#f4e7cf';
  const dark = '#090817';

  context.scale(1.2 * scaleMultiplier, 1.2 * scaleMultiplier);

  context.fillStyle = primary;
  context.fillRect(-10, -22, 6, 12);
  context.fillRect(4, -22, 6, 12);
  context.fillStyle = skin;
  context.fillRect(-8, -20, 2, 8);
  context.fillRect(6, -20, 2, 8);

  context.fillStyle = primary;
  context.fillRect(-14, -12, 28, 22);

  context.fillStyle = furAccent;
  context.fillRect(-14, -12, 28, 4);
  context.fillRect(-16, -8, 4, 8);
  context.fillRect(10, -8, 4, 8);

  context.fillStyle = skin;
  context.fillRect(2, -2, 18, 12);

  context.fillStyle = dark;
  context.fillRect(14, 0, 4, 3);

  context.fillRect(10, 5, 8, 2);
  if (chew) {
    context.fillRect(10, 7, 6, 2);
  }

  context.fillStyle = dark;
  context.fillRect(-2, -6, 6, 6);
  context.fillStyle = '#ffffff';
  context.fillRect(0, -4, 2, 2);

  context.restore();
}

function drawEnemy(
  context: CanvasRenderingContext2D,
  x: number,
  y: number,
  width: number,
  height: number,
  time: number,
  index: number
) {
  const enemySize = Math.max(30, width);
  const count = Math.max(1, Math.round(height / enemySize));
  const spacing = height / count;

  context.save();
  for(let i = 0; i < count; i++) {
    const cy = y + (i + 0.5) * spacing;
    const cx = x + width / 2;
    const wobble = Math.sin(time * 0.005 + i + index) * 4;
    const scale = 1 + Math.sin(time * 0.01 + i) * 0.1;

    context.translate(cx + wobble, cy);
    context.scale(scale, 1/scale);

    const w = width * 1.2;
    const h = spacing * 0.8;

    context.fillStyle = '#ff2ca8';
    context.fillRect(-w/2, -h/2, w, h*0.7);

    const frame = Math.floor(time / 150) % 2;
    if (frame === 0) {
      context.fillRect(-w/2, h*0.2, w*0.25, h*0.3);
      context.fillRect(-w/8, h*0.2, w*0.25, h*0.3);
      context.fillRect(w/4, h*0.2, w*0.25, h*0.3);
    } else {
      context.fillRect(-w/3, h*0.2, w*0.25, h*0.3);
      context.fillRect(w/12, h*0.2, w*0.25, h*0.3);
    }

    context.fillStyle = '#ffffff';
    context.fillRect(-w/3, -h/4, w*0.25, h*0.25);
    context.fillRect(w/12, -h/4, w*0.25, h*0.25);

    const look = Math.sin(time * 0.003 + index) > 0 ? 1 : -1;
    context.fillStyle = '#090817';
    context.fillRect(-w/3 + w*0.1 + look*2, -h/4 + h*0.1, w*0.1, h*0.1);
    context.fillRect(w/12 + w*0.1 + look*2, -h/4 + h*0.1, w*0.1, h*0.1);

    context.scale(1/scale, scale);
    context.translate(-(cx + wobble), -cy);
  }
  context.restore();
}

function drawCollectible(
  context: CanvasRenderingContext2D,
  collectible: ArcadeCollectible,
  cameraX: number,
  width: number,
  height: number,
  time: number,
) {
  const x = collectible.x * WORLD_WIDTH - cameraX;
  if (x < -30 || x > width + 30) return;
  const y = 82 + collectible.y * Math.max(120, height - 180);
  const pulse = 2 + Math.sin(time * 0.006 + collectible.x * 20) * 2;

  context.fillStyle = 'rgba(255, 228, 92, .16)';
  context.fillRect(x - 12 - pulse, y - 12 - pulse, 24 + pulse * 2, 24 + pulse * 2);
  context.fillStyle = '#ffe45c';
  context.fillRect(x - 7, y - 7, 14, 14);
  context.fillStyle = '#ffffff';
  context.fillRect(x - 3, y - 3, 6, 6);
}

function levelOneY(normalizedY: number, height: number) {
  return 82 + normalizedY * Math.max(120, height - 180);
}

const LEVEL_TWO_PLATFORMS = [
  { x: 0.1, y: 0.9, width: 0.3, motion: 0.04, speed: 0.7 },
  { x: 0.52, y: 0.78, width: 0.27, motion: 0.08, speed: 0.9 },
  { x: 0.18, y: 0.65, width: 0.24, motion: 0.1, speed: 1.1 },
  { x: 0.6, y: 0.51, width: 0.25, motion: 0.09, speed: 0.8 },
  { x: 0.26, y: 0.37, width: 0.22, motion: 0.12, speed: 1.2 },
  { x: 0.58, y: 0.23, width: 0.25, motion: 0.08, speed: 1 },
  { x: 0.38, y: 0.1, width: 0.25, motion: 0.05, speed: 0.75 },
] as const;

function drawLevelTwo(
  context: CanvasRenderingContext2D,
  dimensions: Dimensions,
  state: ArcadeState,
  player: ArcadePlayer,
  visualPosition: { x: number; y: number },
  cameraX: number,
  cameraY: number,
  time: number,
  facing: number,
  showClear: boolean,
) {
  const { width, height } = dimensions;
  const worldToScreen = (x: number, y: number) => ({
    x: x * LEVEL_TWO_WORLD_WIDTH - cameraX,
    y: y * LEVEL_TWO_WORLD_HEIGHT - cameraY,
  });

  context.fillStyle = '#080525';
  context.fillRect(0, 0, width, height);

  context.strokeStyle = 'rgba(53, 231, 255, .18)';
  context.lineWidth = 1;
  const gridSize = 48;
  for (let x = -(cameraX % gridSize); x < width; x += gridSize) {
    context.beginPath();
    context.moveTo(Math.round(x), 0);
    context.lineTo(Math.round(x), height);
    context.stroke();
  }
  for (let y = -(cameraY % gridSize); y < height; y += gridSize) {
    context.beginPath();
    context.moveTo(0, Math.round(y));
    context.lineTo(width, Math.round(y));
    context.stroke();
  }

  for (let index = 0; index < 28; index += 1) {
    const worldX = (index * 191 + 83) % LEVEL_TWO_WORLD_WIDTH;
    const worldY = (index * 337 + 101) % LEVEL_TWO_WORLD_HEIGHT;
    const point = { x: worldX - cameraX, y: worldY - cameraY };
    if (point.x < 0 || point.x > width || point.y < 0 || point.y > height) continue;
    context.fillStyle = index % 2 ? 'rgba(239, 45, 168, .35)' : 'rgba(53, 231, 255, .3)';
    context.fillRect(point.x, point.y, 18 + (index % 3) * 8, 4);
  }

  LEVEL_TWO_PLATFORMS.forEach((platform, index) => {
    const movingX = clamp(
      platform.x + Math.sin(time * 0.001 * platform.speed + index) * platform.motion,
      0.04,
      0.96 - platform.width,
    );
    const point = worldToScreen(movingX, platform.y);
    const platformWidth = platform.width * LEVEL_TWO_WORLD_WIDTH;
    if (point.y < -30 || point.y > height + 30 || point.x + platformWidth < 0 || point.x > width) return;
    context.fillStyle = 'rgba(53, 231, 255, .18)';
    context.fillRect(point.x - 5, point.y - 8, platformWidth + 10, 26);
    context.fillStyle = index % 2 ? '#ef2da8' : '#35e7ff';
    context.fillRect(point.x, point.y, platformWidth, 8);
    context.fillStyle = '#21154e';
    for (let tileX = 0; tileX < platformWidth; tileX += 22) {
      context.fillRect(point.x + tileX, point.y + 8, 16, 10);
    }
  });

  state.logs.filter((hazard) => hazard.active).forEach((hazard, index) => {
    const point = worldToScreen(hazard.x, hazard.y);
    const size = Math.max(28, hazard.radius * LEVEL_TWO_WORLD_WIDTH * 1.5);
    if (point.x < -size || point.x > width + size || point.y < -size || point.y > height + size) return;
    const flicker = 0.55 + Math.sin(time * 0.018 + index * 2.1) * 0.25;
    context.fillStyle = `rgba(239, 45, 168, ${flicker * 0.28})`;
    context.fillRect(point.x - size, point.y - size, size * 2, size * 2);
    context.fillStyle = '#ff2ca8';
    context.fillRect(point.x - size * 0.72, point.y - 5, size * 1.44, 10);
    context.fillStyle = '#ffffff';
    for (let bolt = -1; bolt <= 1; bolt += 1) {
      context.fillRect(point.x + bolt * 12 - 2, point.y - size * 0.65, 4, size * 1.3);
    }
  });

  Object.values(state.collectibles).forEach((collectible) => {
    const point = worldToScreen(collectible.x, collectible.y);
    if (point.x < -24 || point.x > width + 24 || point.y < -24 || point.y > height + 24) return;
    const pulse = 7 + Math.sin(time * 0.007 + collectible.x * 10) * 2;
    context.fillStyle = 'rgba(255, 228, 92, .24)';
    context.fillRect(point.x - pulse - 5, point.y - pulse - 5, (pulse + 5) * 2, (pulse + 5) * 2);
    context.fillStyle = '#ffe45c';
    context.fillRect(point.x - 7, point.y - 7, 14, 14);
  });

  const portal = worldToScreen(0.5, 0.045);
  if (portal.y > -120 && portal.y < height + 120) {
    const pulse = 1 + Math.sin(time * 0.006) * 0.1;
    context.save();
    context.translate(portal.x, portal.y);
    context.scale(pulse, pulse);
    context.strokeStyle = '#35e7ff';
    context.lineWidth = 9;
    context.strokeRect(-42, -55, 84, 110);
    context.strokeStyle = '#ef2da8';
    context.lineWidth = 4;
    context.strokeRect(-29, -42, 58, 84);
    context.fillStyle = 'rgba(80, 245, 150, .22)';
    context.fillRect(-24, -37, 48, 74);
    context.restore();
    context.fillStyle = '#50f596';
    context.font = 'bold 11px "Space Mono", monospace';
    context.textAlign = 'center';
    context.fillText('QUANTUM PORTAL', clamp(portal.x, 64, width - 64), portal.y - 68);
  }

  state.players.forEach((candidate) => {
    const self = candidate.player_id === player.player_id;
    const point = worldToScreen(
      self ? visualPosition.x : candidate.x,
      self ? visualPosition.y : candidate.y,
    );
    if (point.x < -50 || point.x > width + 50 || point.y < -60 || point.y > height + 60) return;
    drawPixelLlama(
      context,
      point.x,
      point.y,
      self,
      self ? facing : 1,
      time,
      self && Math.abs(player.x - visualPosition.x) > 0.001,
    );
    context.fillStyle = '#ffffff';
    context.font = '10px "Space Mono", monospace';
    context.textAlign = 'center';
    context.fillText(candidate.name, clamp(point.x, 32, width - 32), point.y - 41);
  });

  const compact = width < 640;
  context.fillStyle = 'rgba(0, 0, 0, .72)';
  context.fillRect(12, 12, compact ? 248 : 350, 42);
  context.fillStyle = '#35e7ff';
  context.font = '11px "Space Mono", monospace';
  context.textAlign = 'left';
  context.fillText('LEVEL 2  •  SYNTH-GRID ASCENT', 21, 27);
  context.fillStyle = '#ffe45c';
  context.fillText(compact ? 'CLIMB  •  DODGE  •  JUMP' : 'ASCEND TO THE QUANTUM PORTAL', 21, 44);

  if (showClear) {
    context.fillStyle = 'rgba(8, 5, 37, .86)';
    context.fillRect(0, height * 0.36, width, 112);
    context.fillStyle = '#50f596';
    context.font = `bold ${compact ? 20 : 30}px "Space Mono", monospace`;
    context.textAlign = 'center';
    context.fillText('ASCENT COMPLETE', width / 2, height * 0.36 + 50);
    context.fillStyle = '#35e7ff';
    context.font = '12px "Space Mono", monospace';
    context.fillText('SERVER CONFIRMED', width / 2, height * 0.36 + 78);
  }
}

function drawLevelOne(
  context: CanvasRenderingContext2D,
  dimensions: Dimensions,
  state: ArcadeState,
  player: ArcadePlayer | undefined,
  visualPosition: { x: number; y: number },
  cameraX: number,
  time: number,
  facing: number,
) {
  const { width, height } = dimensions;
  const isMobile = width < 640;

  const topMargin = isMobile ? 120 : 100;
  const bottomMargin = isMobile ? 180 : 160;
  const corridorHeight = Math.max(180, height - topMargin - bottomMargin);
  const corridorTop = topMargin;
  const corridorBottom = corridorTop + corridorHeight;

  function toScreenX(worldX: number) {
    return worldX * WORLD_WIDTH - cameraX;
  }
  function toScreenY(normalizedY: number) {
    return corridorTop + normalizedY * corridorHeight;
  }

  context.fillStyle = '#0a0a1a';
  context.fillRect(0, corridorTop, width, corridorHeight);

  // Grid
  context.strokeStyle = 'rgba(53, 231, 255, 0.1)';
  context.lineWidth = 1;
  const gridSize = 40;
  const gridOffset = -(cameraX % gridSize);
  for (let x = gridOffset; x < width; x += gridSize) {
    context.beginPath();
    context.moveTo(Math.round(x), corridorTop);
    context.lineTo(Math.round(x), corridorBottom);
    context.stroke();
  }
  for (let y = corridorTop; y <= corridorBottom; y += gridSize) {
    context.beginPath();
    context.moveTo(0, Math.round(y));
    context.lineTo(width, Math.round(y));
    context.stroke();
  }

  // Subtle Pac-Man-like route dots
  context.save();
  context.strokeStyle = 'rgba(53, 231, 255, 0.4)';
  context.lineWidth = 4;
  context.lineCap = 'round';
  context.setLineDash?.([0, 30]);
  context.lineDashOffset = -time * 0.02;
  context.beginPath();
  const route = [
    [0.0, 0.5],
    [0.12, 0.5],
    [0.12, 0.28],
    [0.31, 0.28],
    [0.31, 0.72],
    [0.5, 0.72],
    [0.5, 0.28],
    [0.68, 0.28],
    [0.68, 0.72],
    [0.85, 0.72],
    [0.85, 0.5],
    [0.95, 0.5],
  ];
  context.moveTo(toScreenX(route[0][0]), toScreenY(route[0][1]));
  for (let i = 1; i < route.length; i++) {
    context.lineTo(toScreenX(route[i][0]), toScreenY(route[i][1]));
  }
  context.stroke();
  context.restore();

  // Maze Walls top and bottom
  const wallHeight = 24;
  context.fillStyle = '#111122';
  context.fillRect(0, corridorTop - wallHeight, width, wallHeight);
  context.fillRect(0, corridorBottom, width, wallHeight);
  context.fillStyle = '#35e7ff';
  context.fillRect(0, corridorTop - 2, width, 2);
  context.fillRect(0, corridorBottom, width, 2);

  // Hazards as Wall Segments + Enemies
  state.levelOne.hazards.forEach((hazard, index) => {
    const screenX = toScreenX(hazard.x);
    const screenW = hazard.width * WORLD_WIDTH;
    if (screenX > width + 40 || screenX + screenW < -40) return;
    const top = toScreenY(hazard.y);
    const bottom = toScreenY(hazard.y + hazard.height);
    const screenH = bottom - top;

    // Maze Wall Block
    context.fillStyle = '#111122';
    context.fillRect(screenX, top, screenW, screenH);
    context.strokeStyle = '#ef2da8';
    context.lineWidth = 2;
    context.strokeRect(screenX, top, screenW, screenH);

    // Hazard visual representation
    context.fillStyle = 'rgba(255, 48, 88, 0.15)';
    context.fillRect(screenX + 2, top + 2, screenW - 4, screenH - 4);

    drawEnemy(context, screenX, top, screenW, screenH, time, index);
  });

  // Collectible TLAMA tokens
  state.levelOne.items.forEach((item) => {
    if (state.levelOne.collectedIds.includes(item.id)) return;
    const screenX = toScreenX(item.x);
    if (screenX > width + 40 || screenX < -40) return;
    const screenY = toScreenY(item.y);

    const pulse = 3 + Math.sin(time * 0.005 + item.x * 20) * 2;
    context.fillStyle = 'rgba(255, 190, 35, 0.34)';
    context.beginPath();
    context.arc(screenX, screenY, 18 + pulse, 0, Math.PI * 2);
    context.fill();

    context.fillStyle = '#ffb51b';
    context.fillRect(screenX - 10, screenY - 15, 20, 30);
    context.fillRect(screenX - 15, screenY - 10, 30, 20);
    context.fillStyle = '#ffe45c';
    context.fillRect(screenX - 9, screenY - 12, 18, 24);
    context.fillRect(screenX - 12, screenY - 9, 24, 18);
    context.fillStyle = '#8c4f00';
    context.fillRect(screenX - 8, screenY - 8, 16, 16);
    context.fillStyle = '#fff1a8';
    context.font = 'bold 9px "Space Mono", monospace';
    context.textAlign = 'center';
    context.fillText('TL', screenX, screenY + 3);
    const sheenX = screenX - 9 + ((time * 0.015 + item.x * 100) % 18);
    context.fillStyle = 'rgba(255, 255, 255, 0.8)';
    context.fillRect(sheenX, screenY - 11, 2, 22);
  });

  // Gate
  const gateX = toScreenX(state.levelOne.finishX);
  const unlocked = state.levelOne.collectedIds.length === state.levelOne.items.length;
  const count = state.levelOne.collectedIds.length;
  const total = state.levelOne.items.length;

  if (gateX > -100 && gateX < width) {
    const gateWidth = 60;

    context.fillStyle = '#111122';
    context.fillRect(gateX, corridorTop - 10, gateWidth, corridorHeight + 20);

    if (!unlocked) {
      context.fillStyle = 'rgba(239, 45, 168, 0.3)';
      context.fillRect(gateX + 20, corridorTop, 20, corridorHeight);
      context.fillStyle = '#ef2da8';
      for (let ly = corridorTop + 20; ly < corridorBottom; ly += 40) {
        context.fillRect(gateX + 10, ly, 40, 6);
      }
      context.fillStyle = '#ffe45c';
      context.font = 'bold 16px "Space Mono", monospace';
      context.textAlign = 'center';
      const gateLabelX = clamp(gateX + gateWidth / 2, 40, width - 40);
      context.font = 'bold 12px "Space Mono", monospace';
      context.fillText('LOCKED', gateLabelX, corridorTop - 20);
    } else {
      context.fillStyle = 'rgba(80, 245, 150, 0.2)';
      context.fillRect(gateX, corridorTop, gateWidth, corridorHeight);
      context.fillStyle = '#50f596';
      context.fillRect(gateX, corridorTop, 4, corridorHeight);
      context.fillRect(gateX + gateWidth - 4, corridorTop, 4, corridorHeight);
      context.font = 'bold 18px "Space Mono", monospace';
      context.textAlign = 'center';
      const gateLabelX = clamp(gateX + gateWidth / 2, 62, width - 62);
      context.fillText('EXIT OPEN', gateLabelX, corridorTop - 20);
      const arrowOffset = (time * 0.05) % 20;
      context.font = '24px "Space Mono", monospace';
      context.fillText('>>', gateX + gateWidth / 2 + arrowOffset - 10, corridorTop + corridorHeight / 2);
    }
  } else if (gateX >= width) {
    // Edge-of-screen EXIT direction indicator
    const indY = corridorTop + corridorHeight / 2;
    context.fillStyle = 'rgba(8, 5, 37, 0.85)';
    const indicatorWidth = Math.min(120, width - 20);
    const indicatorX = width - indicatorWidth;
    context.fillRect(indicatorX, indY - 30, indicatorWidth, 60);

    context.strokeStyle = unlocked ? '#50f596' : '#ffe45c';
    context.lineWidth = 2;
    context.strokeRect(indicatorX, indY - 30, indicatorWidth, 60);

    context.fillStyle = unlocked ? '#50f596' : '#ffe45c';
    context.font = 'bold 11px "Space Mono", monospace';
    context.textAlign = 'right';
    context.fillText(unlocked ? 'EXIT OPEN' : 'EXIT LOCKED', width - 15, indY - 5);

    context.font = 'bold 16px "Space Mono", monospace';
    const arrowOffset = (time * 0.05) % 10;
    context.fillText('EXIT >>', width - 20 - arrowOffset, indY + 15);
  }

  // Players
  state.players.forEach((candidate) => {
    const self = candidate.player_id === player?.player_id;
    if (!self && candidate.current_level !== 1) return;

    const worldX = (self ? visualPosition.x : candidate.x);
    const screenX = toScreenX(worldX);
    if (screenX < -60 || screenX > width + 60) return;

    const normalizedY = self ? visualPosition.y : candidate.y;
    const screenY = toScreenY(normalizedY);
    const spritePadding = self ? 54 : 32;
    const renderedY = clamp(
      screenY,
      corridorTop + spritePadding,
      corridorBottom - spritePadding,
    );

    drawPixelLlama(
      context,
      screenX,
      renderedY,
      self,
      self ? facing : 1,
      time,
      self && player ? Math.abs(player.x - visualPosition.x) > 0.001 || Math.abs(player.y - visualPosition.y) > 0.001 : false,
      self ? 2.2 : 1.2,
      true,
    );

    context.fillStyle = '#ffffff';
    context.font = '10px "Space Mono", monospace';
    context.textAlign = 'center';
    context.fillText(candidate.name, screenX, renderedY - (self ? 45 : 30));
  });

  const progressWidth = Math.min(228, width - 24);
  const progressX = 12;
  const progressY = corridorBottom - 38;
  context.fillStyle = 'rgba(8, 5, 37, 0.94)';
  context.fillRect(progressX, progressY, progressWidth, 26);
  context.strokeStyle = '#ffb51b';
  context.lineWidth = 2;
  context.strokeRect(progressX, progressY, progressWidth, 26);
  context.fillStyle = '#ffe45c';
  context.font = 'bold 10px "Space Mono", monospace';
  context.textAlign = 'center';
  context.fillText(
    `TLAMA TOKENS COLLECTED ${count}/${total}`,
    progressX + progressWidth / 2,
    progressY + 17,
  );
}

export function drawWorld(
  context: CanvasRenderingContext2D,
  dimensions: Dimensions,
  state: ArcadeState,
  player: ArcadePlayer | undefined,
  visualPosition: { x: number; y: number },
  cameraX: number,
  cameraY: number,
  time: number,
  facing: number,
  showLevelTwoClear = false,
) {
  if (player?.current_level === 1) {
    drawLevelOne(context, dimensions, state, player, visualPosition, cameraX, time, facing);
    return;
  }

  const { width, height } = dimensions;
  const groundY = height - 46;

  context.fillStyle = '#070714';
  context.fillRect(0, 0, width, height);

  if (player?.current_level === 2 || showLevelTwoClear) {
    if (player) {
      drawLevelTwo(
        context,
        dimensions,
        state,
        player,
        visualPosition,
        cameraX,
        cameraY,
        time,
        facing,
        showLevelTwoClear,
      );
    }
    return;
  }

  const starsOffset = cameraX * 0.08;
  for (let index = 0; index < 42; index += 1) {
    const worldX = (index * 173 + 61) % WORLD_WIDTH;
    const x = ((worldX - starsOffset) % (width + 80)) - 40;
    const y = 30 + ((index * 71) % Math.max(80, Math.floor(height * 0.48)));
    context.fillStyle = index % 5 === 0 ? '#ffe45c' : '#35e7ff';
    context.fillRect(Math.round(x), y, index % 3 === 0 ? 3 : 2, 2);
  }

  const skylineLayers = [
    { speed: 0.12, base: groundY - 126, color: '#15113b', step: 112 },
    { speed: 0.24, base: groundY - 82, color: '#21154e', step: 82 },
  ];
  skylineLayers.forEach((layer, layerIndex) => {
    const offset = -((cameraX * layer.speed) % layer.step);
    context.fillStyle = layer.color;
    for (let x = offset - layer.step; x < width + layer.step; x += layer.step) {
      const buildingHeight = 46 + ((Math.abs(Math.floor(x / layer.step)) * 29 + layerIndex * 13) % 72);
      context.fillRect(Math.round(x), layer.base - buildingHeight, layer.step - 12, buildingHeight);
      context.fillStyle = 'rgba(239, 45, 168, .25)';
      for (let windowY = layer.base - buildingHeight + 12; windowY < layer.base - 8; windowY += 18) {
        context.fillRect(Math.round(x + 13), windowY, 5, 5);
        context.fillRect(Math.round(x + 33), windowY, 5, 5);
      }
      context.fillStyle = layer.color;
    }
  });

  context.strokeStyle = 'rgba(42, 246, 255, .15)';
  context.lineWidth = 1;
  const gridOffset = -(cameraX % 48);
  for (let x = gridOffset; x < width; x += 48) {
    context.beginPath();
    context.moveTo(Math.round(x), groundY - 12);
    context.lineTo(Math.round(x), height);
    context.stroke();
  }
  for (let y = groundY - 12; y < height; y += 16) {
    context.beginPath();
    context.moveTo(0, y);
    context.lineTo(width, y);
    context.stroke();
  }

  context.fillStyle = '#ef2da8';
  context.fillRect(0, groundY, width, 8);
  context.fillStyle = '#341544';
  for (let x = -(cameraX % 32); x < width + 32; x += 32) {
    context.fillRect(Math.round(x), groundY + 8, 24, 28);
  }

  // Legacy level 1 logic removed

  const signWorldPositions = [470, 1120, 1810, 2260];
  signWorldPositions.forEach((worldX, index) => {
    const x = worldX - cameraX;
    if (x < -120 || x > width + 120) return;
    context.fillStyle = '#0c0b1d';
    context.fillRect(x - 4, groundY - 89, 8, 89);
    context.fillStyle = index % 2 === 0 ? '#35e7ff' : '#ef2da8';
    context.fillRect(x - 47, groundY - 102, 94, 32);
    context.fillStyle = '#080816';
    context.font = 'bold 11px "Space Mono", monospace';
    context.textAlign = 'center';
    drawBoundedCenteredLabel(
      context,
      index === 3 ? 'CHECKPOINT' : 'ORBS AHEAD',
      x,
      groundY - 81,
      width,
    );
  });

  Object.values(state.collectibles).forEach((collectible) => {
    drawCollectible(context, collectible, cameraX, width, height, time);
  });

  state.players.forEach((candidate) => {
    const self = candidate.player_id === player?.player_id;
    const worldX = (self ? visualPosition.x : candidate.x) * WORLD_WIDTH;
    const x = worldX - cameraX;
    if (x < -60 || x > width + 60) return;
    const normalizedY = self ? visualPosition.y : candidate.y;
    const y = clamp(
      82 + normalizedY * Math.max(120, height - 180),
      86,
      groundY - PLAYER_HEIGHT / 2,
    );
    drawPixelLlama(
      context,
      x,
      y,
      self,
      self ? facing : 1,
      time,
      self && player ? Math.abs(player.x - visualPosition.x) > 0.001 : false,
    );
    context.fillStyle = '#ffffff';
    context.font = '10px "Space Mono", monospace';
    context.textAlign = 'center';
    context.fillText(candidate.name, x, y - 41);
  });

  if (player) {
    const level = state.levels.find(({ number }) => number === player.current_level);
    if (!level) return;

    const compact = width < 640;
    const panelWidth = Math.max(1, Math.min(compact ? width - HUD_MARGIN * 2 : 390, width - HUD_MARGIN * 2));
    const labelY = compact ? 12 : Math.max(170, groundY - 37);
    const textMaxWidth = Math.max(1, panelWidth - 18);
    context.fillStyle = 'rgba(0, 0, 0, .66)';
    context.fillRect(HUD_MARGIN, labelY, panelWidth, 42);
    context.fillStyle = '#35e7ff';
    context.font = '11px "Space Mono", monospace';
    context.textAlign = 'left';
    context.fillText(
      `LEVEL ${level.number}  •  ${level.name.toUpperCase()}`,
      HUD_MARGIN + 9,
      labelY + 15,
      textMaxWidth,
    );
    context.fillStyle = player.victory_announced ? '#50f596' : '#ffe45c';
    const status = player.victory_announced
      ? 'CITADEL CLEAR  •  GAME COMPLETE'
      : compact
        ? `SCORE ${player.score}/${level.next_score}  •  ADVANCE`
        : `PROGRESS ${player.score}/${level.next_score}  •  REACH NEXT STAGE`;
    context.fillText(status, HUD_MARGIN + 9, labelY + 32, textMaxWidth);
  }
}

export function Playfield() {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const frameRef = useRef<number | null>(null);
  const stateRef = useRef<ArcadeState | null>(null);
  const playerRef = useRef<ArcadePlayer | undefined>(undefined);
  const dimensionsRef = useRef<Dimensions>({ width: 800, height: 600 });
  const visualXRef = useRef<VisualAxis>({ position: 0.5, velocity: 0 });
  const visualYRef = useRef<VisualAxis>({ position: 0.5, velocity: 0 });
  const cameraRef = useRef(0);
  const cameraYRef = useRef(0);
  const facingRef = useRef(1);
  const lastTargetXRef = useRef(0.5);
  const previousLevelRef = useRef(1);
  const levelTwoClearUntilRef = useRef(0);
  const lastFrameTimeRef = useRef<number | null>(null);
  const { state, player } = useArcadeEngine();
  const [dimensions, setDimensions] = useState<Dimensions>({ width: 800, height: 600 });

  stateRef.current = state;
  playerRef.current = player;
  dimensionsRef.current = dimensions;

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const observer = new ResizeObserver(([entry]) => {
      if (!entry) return;
      setDimensions({
        width: Math.max(1, entry.contentRect.width),
        height: Math.max(1, entry.contentRect.height),
      });
    });
    observer.observe(container);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const dpr = Math.max(1, window.devicePixelRatio || 1);
    canvas.width = Math.round(dimensions.width * dpr);
    canvas.height = Math.round(dimensions.height * dpr);
    canvas.style.width = `${dimensions.width}px`;
    canvas.style.height = `${dimensions.height}px`;
  }, [dimensions]);

  useEffect(() => {
    const render = (time: number) => {
      const previousTime = lastFrameTimeRef.current ?? time;
      const deltaSeconds = (time - previousTime) / 1000;
      lastFrameTimeRef.current = time;
      const canvas = canvasRef.current;
      const currentState = stateRef.current;
      if (!canvas || !currentState) {
        frameRef.current = requestAnimationFrame(render);
        return;
      }
      const context = canvas.getContext('2d');
      if (!context) return;
      const currentPlayer = playerRef.current;
      const currentDimensions = dimensionsRef.current;
      const dpr = Math.max(1, window.devicePixelRatio || 1);
      context.setTransform(dpr, 0, 0, dpr, 0, 0);
      context.imageSmoothingEnabled = false;

      if (currentPlayer) {
        if (previousLevelRef.current === 2 && currentPlayer.current_level > 2) {
          levelTwoClearUntilRef.current = time + 2200;
        }
        previousLevelRef.current = currentPlayer.current_level;
        if (Math.abs(currentPlayer.x - lastTargetXRef.current) > 0.001) {
          facingRef.current = currentPlayer.x > lastTargetXRef.current ? 1 : -1;
          lastTargetXRef.current = currentPlayer.x;
        }
        const isLevelOne = currentPlayer.current_level === 1;
        visualXRef.current = smoothVisualAxis(
          visualXRef.current,
          currentPlayer.x,
          deltaSeconds,
          { stiffness: 190, damping: 16, snapDistance: isLevelOne ? 1.5 : 0.24 },
        );
        visualYRef.current = smoothVisualAxis(
          visualYRef.current,
          currentPlayer.y,
          deltaSeconds,
          { stiffness: 230, damping: 19, snapDistance: isLevelOne ? 1.5 : 0.2 },
        );
      }

      const visualPosition = {
        x: visualXRef.current.position,
        y: visualYRef.current.position,
      };

      const levelTwo = currentPlayer?.current_level === 2 || time < levelTwoClearUntilRef.current;
      const activeWorldWidth = levelTwo ? LEVEL_TWO_WORLD_WIDTH : WORLD_WIDTH;
      const maxCamera = Math.max(0, activeWorldWidth - currentDimensions.width);
      const desiredCamera = currentPlayer
        ? clamp(visualPosition.x * activeWorldWidth - currentDimensions.width * 0.5, 0, maxCamera)
        : 0;
      cameraRef.current += (desiredCamera - cameraRef.current) * 0.09;
      const maxCameraY = Math.max(0, LEVEL_TWO_WORLD_HEIGHT - currentDimensions.height);
      const desiredCameraY = levelTwo && currentPlayer
        ? clamp(visualPosition.y * LEVEL_TWO_WORLD_HEIGHT - currentDimensions.height * 0.58, 0, maxCameraY)
        : 0;
      cameraYRef.current += (desiredCameraY - cameraYRef.current) * 0.1;

      drawWorld(
        context,
        currentDimensions,
        currentState,
        currentPlayer,
        visualPosition,
        cameraRef.current,
        cameraYRef.current,
        time,
        facingRef.current,
        time < levelTwoClearUntilRef.current,
      );
      frameRef.current = requestAnimationFrame(render);
    };

    frameRef.current = requestAnimationFrame(render);
    return () => {
      if (frameRef.current !== null) cancelAnimationFrame(frameRef.current);
    };
  }, []);

  return (
    <div
      ref={containerRef}
      className="w-full h-full relative bg-black overflow-hidden border-4 border-primary retro-shadow touch-none select-none"
      data-testid="container-playfield"
    >
      <div className="absolute inset-0 crt-overlay pointer-events-none z-[1]" />
      <canvas
        ref={canvasRef}
        className="block"
        style={{ imageRendering: 'pixelated', touchAction: 'none' }}
        data-testid="canvas-game"
      />
    </div>
  );
}