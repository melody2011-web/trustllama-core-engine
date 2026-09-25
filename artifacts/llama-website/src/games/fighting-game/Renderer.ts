export interface MatchState {
  match_id: string;
  status: string;
  round_seconds_remaining: number;
  winner_slot: string | null;
  revision: number;
  entry_validation: any;
  players: Record<string, PlayerState>;
}

export interface PlayerState {
  player_id: string;
  slot: string;
  display_name: string;
  character_id: string | null;
  health: number;
  combo: { active: boolean; hits: number; total_damage: number };
  connected: boolean;
  position_x?: number;
  position_y?: number;
  facing?: "left" | "right";
  animation_state?: string;
  blocking?: boolean;
  is_computer?: boolean;
}

const ANIMATIONS: Record<string, number[]> = {
  idle: [5, -5, 15, -25, -15, -45, 15, -15, -15, 15],
  move_left: [10, 0, 30, -10, -30, -30, -30, 0, 30, 0],
  move_right: [10, 0, 30, -10, -30, -30, -30, 0, 30, 0],
  crouch: [35, -25, 40, -40, -20, -70, 100, -130, 100, -130],
  jump: [-15, 15, -150, -10, -130, -30, -20, -10, 10, -10],
  light_punch: [15, -5, -90, -10, -40, -60, 15, -15, -10, 10],
  heavy_kick: [-15, 15, -40, -50, -70, -30, -100, 10, -15, 15],
  block: [0, 10, -130, -140, -110, -120, 25, -25, 0, 0],
  hit: [-30, -40, 50, 30, 30, 20, -25, 5, -35, 5],
  knockout: [-90, -90, -180, 0, -180, 0, 0, 0, 0, 0]
};

interface FighterSnapshot {
  x: number;
  y: number;
  angles: number[];
  facing: 1 | -1;
  state: string;
}

class LocalFighter {
  x: number = 0;
  y: number = 240;
  angles: number[] = [...ANIMATIONS.idle];
  facing: 1 | -1 = 1;
  animTime: number = 0;

  history: FighterSnapshot[] = [];
  historyCount: number = 0;

  constructor() {
     for (let i = 0; i < 8; i++) {
        this.history.push({ x: 0, y: 0, angles: [...ANIMATIONS.idle], facing: 1, state: 'idle' });
     }
  }

  update(p: PlayerState, dt: number, isP1: boolean, immediate = false) {
    const speed = 400 * (dt / 1000);
    const state = p.animation_state || 'idle';

    if (p.position_x !== undefined) {
      this.x += (p.position_x - this.x) * 0.3;
    } else {
      if (state === 'move_left') this.x -= speed;
      if (state === 'move_right') this.x += speed;
      this.x = Math.max(50, Math.min(950, this.x));
    }

    if (p.position_y !== undefined) {
      const canvasY = 240 - p.position_y;
      this.y += (canvasY - this.y) * 0.3;
    } else {
      if (state === 'jump') {
         this.y += (100 - this.y) * 0.4;
      } else {
         this.y += (240 - this.y) * 0.3;
      }
    }

    if (p.facing) {
      this.facing = p.facing === 'left' ? -1 : 1;
    } else {
      this.facing = isP1 ? 1 : -1;
      if (state === 'move_left') this.facing = -1;
      if (state === 'move_right') this.facing = 1;
    }

    const targetAngles = ANIMATIONS[state] || ANIMATIONS.idle;
    this.animTime += dt;

    for (let i = 0; i < this.angles.length; i++) {
      let t = targetAngles[i];
      if (state === 'idle') {
         if (i === 0) t += Math.sin(this.animTime * 0.003) * 4;
         if (i === 2 || i === 4) t += Math.sin(this.animTime * 0.003 + Math.PI) * 5;
         if (i >= 6) t += Math.sin(this.animTime * 0.003) * 2;
      } else if (state === 'move_left' || state === 'move_right') {
         if (i >= 6) {
            t += Math.sin(this.animTime * 0.015 + (i===6||i===7?0:Math.PI)) * 50;
         }
         if (i >= 2 && i <= 5) {
            t += Math.sin(this.animTime * 0.015 + (i===2||i===3?Math.PI:0)) * 30;
         }
      }
      this.angles[i] = immediate ? t : this.angles[i] + (t - this.angles[i]) * 0.4;
    }

    if (!immediate) {
        const isFast = ['jump', 'move_left', 'move_right', 'light_punch', 'heavy_kick'].includes(state);

        for (let i = this.history.length - 1; i > 0; i--) {
            this.history[i].x = this.history[i-1].x;
            this.history[i].y = this.history[i-1].y;
            this.history[i].facing = this.history[i-1].facing;
            this.history[i].state = this.history[i-1].state;
            for (let a = 0; a < this.angles.length; a++) this.history[i].angles[a] = this.history[i-1].angles[a];
        }

        this.history[0].x = this.x;
        this.history[0].y = this.y;
        this.history[0].facing = this.facing;
        this.history[0].state = state;
        for (let a = 0; a < this.angles.length; a++) this.history[0].angles[a] = this.angles[a];

        if (isFast) {
            this.historyCount = Math.min(8, this.historyCount + 1);
        } else {
            this.historyCount = Math.max(0, this.historyCount - 1);
        }
    } else {
        this.historyCount = 0;
    }
  }
}

class RainDrop {
  x: number = 0;
  y: number = 0;
  len: number = 0;
  speed: number = 0;
  opacity: number = 0;

  constructor(w: number, h: number) {
    this.reset(w, h, true);
  }

  reset(w: number, h: number, randomY: boolean = false) {
    this.x = Math.random() * w;
    this.y = randomY ? Math.random() * h : -50;
    this.len = 20 + Math.random() * 30;
    this.speed = 15 + Math.random() * 15;
    this.opacity = 0.2 + Math.random() * 0.3;
  }
}

export class Renderer {
  private canvas: HTMLCanvasElement;
  private ctx: CanvasRenderingContext2D;
  private animationFrame: number = 0;
  private lastTime: number = 0;
  private f1 = new LocalFighter();
  private f2 = new LocalFighter();
  private rain: RainDrop[] = [];
  private envTime: number = 0;

  public state: MatchState | null = null;
  public localPlayerId: string | null = null;

  constructor(canvas: HTMLCanvasElement) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d', { alpha: false })!;
    this.f1.x = 250;
    this.f2.x = 750;
  }

  public start() {
    this.lastTime = performance.now();
    for (let i = 0; i < 150; i++) {
       this.rain.push(new RainDrop(this.canvas.width, this.canvas.height));
    }
    this.loop(this.lastTime);
  }

  public renderStatic() {
    this.rain = [];
    this.draw(0, true);
  }

  public stop() {
    cancelAnimationFrame(this.animationFrame);
  }

  private loop = (time: number) => {
    this.animationFrame = requestAnimationFrame(this.loop);
    const dt = Math.min(time - this.lastTime, 100);
    this.lastTime = time;
    this.draw(dt);
  }

  private drawEnvironment(w: number, h: number, dt: number, floorY: number, scale: number, offsetX: number, staticFrame = false) {
    const ctx = this.ctx;
    this.envTime += dt;

    // Deep neon night sky
    const sky = ctx.createLinearGradient(0, 0, 0, floorY);
    sky.addColorStop(0, '#020208');
    sky.addColorStop(1, '#0a0b1a');
    ctx.fillStyle = sky;
    ctx.fillRect(0, 0, w, floorY);

    // Distant Buildings (Parallax Layer 1)
    ctx.save();
    ctx.translate(offsetX * 0.2, 0);
    ctx.fillStyle = '#05050f';
    for (let i = -1; i < 5; i++) {
       ctx.fillRect(i * 300 * scale, floorY - 200 * scale, 150 * scale, 200 * scale);
       ctx.fillRect(i * 300 * scale + 160 * scale, floorY - 150 * scale, 120 * scale, 150 * scale);
    }
    ctx.restore();

    // Midground Buildings (Parallax Layer 2)
    ctx.save();
    ctx.translate(offsetX * 0.5, 0);
    for (let i = -1; i < 5; i++) {
       const bx = i * 400 * scale;
       const bw = 200 * scale;
       const bh = 250 * scale;

       ctx.fillStyle = '#080614';
       ctx.fillRect(bx, floorY - bh, bw, bh);

       ctx.fillStyle = 'rgba(0, 255, 255, 0.05)';
       ctx.shadowColor = '#00ffff';
       ctx.shadowBlur = 10;
       for (let wy = floorY - bh + 20 * scale; wy < floorY - 20 * scale; wy += 30 * scale) {
           ctx.fillRect(bx + 20 * scale, wy, 40 * scale, 15 * scale);
           ctx.fillRect(bx + bw - 60 * scale, wy, 40 * scale, 15 * scale);
       }
       ctx.shadowBlur = 0;
    }
    ctx.restore();

    // Ground and reflection
    const ground = ctx.createLinearGradient(0, floorY, 0, h);
    ground.addColorStop(0, '#100a20');
    ground.addColorStop(1, '#020108');
    ctx.fillStyle = ground;
    ctx.fillRect(0, floorY, w, h - floorY);

    // Floor grid / neon lines
    ctx.save();
    ctx.translate(offsetX, 0);
    ctx.beginPath();
    ctx.strokeStyle = 'rgba(236, 72, 153, 0.4)';
    ctx.lineWidth = 3 * scale;
    ctx.shadowColor = '#ec4899';
    ctx.shadowBlur = 15;
    ctx.moveTo(0, floorY);
    ctx.lineTo(1000 * scale, floorY);
    ctx.stroke();

    ctx.beginPath();
    ctx.strokeStyle = 'rgba(0, 255, 255, 0.15)';
    ctx.lineWidth = 1 * scale;
    ctx.shadowBlur = 0;
    for (let i = 0; i <= 1000; i += 100) {
       ctx.moveTo(i * scale, floorY);
       ctx.lineTo((i - 200) * scale, h);
    }
    ctx.stroke();
    ctx.restore();

    // Rain
    ctx.strokeStyle = 'rgba(150, 200, 255, 0.3)';
    ctx.lineWidth = 1.5;
    ctx.lineCap = 'round';
    ctx.beginPath();
    for (const r of this.rain) {
        if (staticFrame) continue;
       r.y += r.speed * (dt / 16);
       r.x -= (r.speed * 0.2) * (dt / 16);
       if (r.y > floorY + Math.random() * 50) {
           r.reset(w, h);
       }
       ctx.globalAlpha = r.opacity;
       ctx.moveTo(r.x, r.y);
       ctx.lineTo(r.x - r.len * 0.2, r.y + r.len);
    }
    ctx.stroke();
    ctx.globalAlpha = 1.0;
  }

  private drawFighterHistory(p: PlayerState, f: LocalFighter, color: string, isBlocking: boolean) {
      if (f.historyCount > 0) {
          for (let i = f.historyCount - 1; i > 0; i--) {
              const alpha = 0.3 * (1 - (i / 8));
              this.drawFighter(p, f, color, isBlocking, f.history[i], alpha);
          }
      }
  }

  private drawFighter(
    p: PlayerState,
    f: LocalFighter,
    color: string,
    isBlocking: boolean,
    snapshot: FighterSnapshot | null = null,
    alpha: number = 1.0,
  ) {
    const ctx = this.ctx;
    ctx.save();
    ctx.globalAlpha = alpha;

    const scaleX = this.canvas.width / 1000;
    const scaleY = this.canvas.height / 320;
    const scale = Math.min(scaleX, scaleY);

    const offsetX = (this.canvas.width - (1000 * scale)) / 2;

    const x = snapshot ? snapshot.x : f.x;
    const y = snapshot ? snapshot.y : f.y;
    const angles = snapshot ? snapshot.angles : f.angles;
    const facing = snapshot ? snapshot.facing : f.facing;
    const state = snapshot ? snapshot.state : (p.animation_state || 'idle');

    const cx = offsetX + x * scale;
    const cy = y * scale;

    ctx.translate(cx, cy);
    ctx.scale(facing * scale, scale);

    let primaryColor = color;
    let glowColor = color;
    let armorColor = '#050505';

    if (state === 'hit') {
       glowColor = '#EF4444';
       primaryColor = '#EF4444';
       armorColor = '#1a0000';
    } else if (state === 'block' || (!snapshot && isBlocking)) {
       glowColor = '#3B82F6';
       primaryColor = '#3B82F6';
    } else if (state === 'knockout') {
       glowColor = 'transparent';
       primaryColor = '#4B5563';
       armorColor = '#111111';
    }

    const [torsoRot, headRot, fArmU, fArmL, bArmU, bArmL, fLegU, fLegL, bLegU, bLegL] = angles;
    const rad = (deg: number) => deg * Math.PI / 180;

    const fillWithHalo = () => {
        if (alpha === 1.0) {
            ctx.strokeStyle = '#2a2a35';
            ctx.lineWidth = 1.5;
            ctx.stroke();
            if (glowColor !== 'transparent') {
                ctx.shadowColor = glowColor;
                ctx.shadowBlur = 8;
            }
        }
        ctx.fill();
        ctx.shadowBlur = 0;
    };

    const drawCyberLimb = (len: number, angleDeg: number, thickness: number, isForearm: boolean = false) => {
       ctx.rotate(rad(angleDeg));

       ctx.fillStyle = armorColor;
       ctx.beginPath();
       const hw = thickness / 2;

       if (isForearm) {
           ctx.moveTo(-hw * 0.8, 0);
           ctx.lineTo(hw * 0.9, 0);
           ctx.quadraticCurveTo(hw * 1.2, len * 0.3, hw * 0.5, len);
           ctx.lineTo(-hw * 0.5, len);
           ctx.quadraticCurveTo(-hw * 0.7, len * 0.3, -hw * 0.8, 0);
       } else {
           ctx.moveTo(-hw, 0);
           ctx.lineTo(hw, 0);
           ctx.quadraticCurveTo(hw * 1.1, len * 0.5, hw * 0.7, len);
           ctx.lineTo(-hw * 0.7, len);
           ctx.quadraticCurveTo(-hw * 0.9, len * 0.5, -hw, 0);
       }
       ctx.closePath();
       fillWithHalo();

       if (glowColor !== 'transparent') {
           ctx.shadowColor = glowColor;
           ctx.shadowBlur = state === 'knockout' ? 0 : (alpha < 1.0 ? 5 : 15);
           ctx.fillStyle = primaryColor;

           ctx.beginPath();
           ctx.arc(0, 0, thickness * 0.25, 0, Math.PI * 2);
           ctx.fill();

           if (isForearm) {
               ctx.fillRect(-hw * 0.2, len * 0.2, hw * 0.4, len * 0.5);
           } else {
               ctx.beginPath();
               ctx.moveTo(-hw * 0.3, len * 0.3);
               ctx.lineTo(hw * 0.3, len * 0.3);
               ctx.lineTo(hw * 0.2, len * 0.7);
               ctx.lineTo(-hw * 0.2, len * 0.7);
               ctx.fill();
           }
           ctx.shadowBlur = 0;
       }

       ctx.translate(0, len);
    };

    ctx.translate(0, -85);
    ctx.lineJoin = 'round';

    // Back leg
    ctx.save();
    drawCyberLimb(50, bLegU, 32);
    drawCyberLimb(55, bLegL, 26, true);
    ctx.fillStyle = armorColor;
    ctx.beginPath();
    ctx.moveTo(-16, 0);
    ctx.lineTo(20, 0);
    ctx.lineTo(28, 20);
    ctx.lineTo(-16, 20);
    ctx.closePath();
    fillWithHalo();
    if (glowColor !== 'transparent') {
        ctx.fillStyle = primaryColor;
        ctx.shadowColor = glowColor;
        ctx.shadowBlur = alpha < 1.0 ? 5 : 15;
        ctx.fillRect(-10, 14, 32, 5);
        ctx.shadowBlur = 0;
    }
    ctx.restore();

    // Back arm
    ctx.save();
    ctx.translate(0, 10);
    drawCyberLimb(45, bArmU - 180, 26);
    drawCyberLimb(45, bArmL, 22, true);
    ctx.fillStyle = armorColor;
    ctx.beginPath(); ctx.arc(0, 5, 16, 0, Math.PI*2);
    fillWithHalo();
    if (glowColor !== 'transparent') {
        ctx.fillStyle = primaryColor;
        ctx.shadowColor = glowColor;
        ctx.shadowBlur = alpha < 1.0 ? 5 : 10;
        ctx.beginPath(); ctx.arc(0, 5, 5, 0, Math.PI*2); ctx.fill();
        ctx.shadowBlur = 0;
    }
    ctx.restore();

    // Torso
    ctx.save();
    ctx.rotate(rad(torsoRot + 180));
    ctx.fillStyle = armorColor;

    ctx.beginPath();
    ctx.moveTo(-28, 0);
    ctx.lineTo(28, 0);
    ctx.quadraticCurveTo(36, 40, 26, 85);
    ctx.lineTo(-26, 85);
    ctx.quadraticCurveTo(-36, 40, -28, 0);
    ctx.closePath();
    fillWithHalo();

    if (glowColor !== 'transparent') {
        ctx.shadowColor = glowColor;
        ctx.shadowBlur = state === 'knockout' ? 0 : (alpha < 1.0 ? 5 : 20);
        ctx.fillStyle = primaryColor;

        ctx.beginPath();
        ctx.moveTo(-10, 20);
        ctx.lineTo(10, 20);
        ctx.lineTo(15, 60);
        ctx.lineTo(0, 75);
        ctx.lineTo(-15, 60);
        ctx.closePath();
        ctx.fill();
        ctx.shadowBlur = 0;
    }
    ctx.translate(0, 85);
    ctx.restore();

    // Front leg
    ctx.save();
    drawCyberLimb(50, fLegU, 34);
    drawCyberLimb(55, fLegL, 28, true);
    ctx.fillStyle = armorColor;
    ctx.beginPath();
    ctx.moveTo(-16, 0);
    ctx.lineTo(20, 0);
    ctx.lineTo(28, 20);
    ctx.lineTo(-16, 20);
    ctx.closePath();
    fillWithHalo();
    if (glowColor !== 'transparent') {
        ctx.fillStyle = primaryColor;
        ctx.shadowColor = glowColor;
        ctx.shadowBlur = alpha < 1.0 ? 5 : 15;
        ctx.fillRect(-10, 14, 32, 5);
        ctx.shadowBlur = 0;
    }
    ctx.restore();

    // Head
    ctx.save();
    ctx.rotate(rad(headRot));
    ctx.translate(0, -10);
    ctx.fillStyle = armorColor;

    ctx.beginPath();
    ctx.moveTo(-16, 20);
    ctx.lineTo(16, 20);
    ctx.lineTo(20, 0);
    ctx.lineTo(14, -18);
    ctx.lineTo(-14, -18);
    ctx.lineTo(-20, 0);
    ctx.closePath();
    fillWithHalo();

    if (glowColor !== 'transparent') {
        ctx.shadowColor = glowColor;
        ctx.shadowBlur = state === 'knockout' ? 0 : (alpha < 1.0 ? 5 : 20);
        ctx.fillStyle = state === 'knockout' ? '#111' : primaryColor;
        ctx.beginPath();
        ctx.moveTo(-5, -3);
        ctx.lineTo(20, 2);
        ctx.lineTo(16, 10);
        ctx.lineTo(-5, 5);
        ctx.closePath();
        ctx.fill();
        ctx.shadowBlur = 0;
    }
    ctx.restore();

    // Front arm
    ctx.save();
    ctx.translate(0, 10);
    drawCyberLimb(45, fArmU - 180, 28);
    drawCyberLimb(45, fArmL, 24, true);
    ctx.fillStyle = armorColor;
    ctx.beginPath(); ctx.arc(0, 5, 18, 0, Math.PI*2);
    fillWithHalo();
    if (glowColor !== 'transparent') {
        ctx.fillStyle = primaryColor;
        ctx.shadowColor = glowColor;
        ctx.shadowBlur = alpha < 1.0 ? 5 : 10;
        ctx.beginPath(); ctx.arc(0, 5, 6, 0, Math.PI*2); ctx.fill();
        ctx.shadowBlur = 0;
    }
    ctx.restore();

    ctx.restore();
  }

  private draw(dt: number, staticFrame = false) {
    const ctx = this.ctx;
    const w = this.canvas.width;
    const h = this.canvas.height;

    if (!this.state || (this.state.status !== 'active' && this.state.status !== 'round_complete')) {
        ctx.fillStyle = '#050510';
        ctx.fillRect(0, 0, w, h);
        return;
    }

    const players = Object.values(this.state.players);
    let p1 = players.find(p => p.player_id === this.localPlayerId) || players[0];
    let p2 = players.find(p => p.player_id !== p1?.player_id) || players[1];

    if (!p1 || !p2) return;

    this.f1.update(p1, dt, true, staticFrame);
    this.f2.update(p2, dt, false, staticFrame);

    const c1 = '#06b6d4'; // Cyan for local P1
    const c2 = '#d946ef'; // Magenta for P2 / AI

    const scaleX = w / 1000;
    const scaleY = h / 320;
    const scale = Math.min(scaleX, scaleY);
    const offsetX = (w - (1000 * scale)) / 2;
    const floorY = 240 * scale;

    this.drawEnvironment(w, h, dt, floorY, scale, offsetX, staticFrame);

    // Draw ghost trails behind fighters
    if (!staticFrame) {
        this.drawFighterHistory(p2, this.f2, c2, !!p2.blocking);
        this.drawFighterHistory(p1, this.f1, c1, !!p1.blocking);
    }

    if (p1.animation_state === 'hit' || p1.animation_state === 'knockout') {
       this.drawFighter(p2, this.f2, c2, !!p2.blocking);
       this.drawFighter(p1, this.f1, c1, !!p1.blocking);
    } else {
       this.drawFighter(p1, this.f1, c1, !!p1.blocking);
       this.drawFighter(p2, this.f2, c2, !!p2.blocking);
    }

    if (p1.animation_state === 'hit' || p2.animation_state === 'hit') {
       ctx.fillStyle = 'rgba(255, 255, 255, 0.05)';
       ctx.fillRect(0, 0, w, h);
    }
  }
}
