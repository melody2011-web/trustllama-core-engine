import type { MatchState, PlayerState } from './Renderer';

export const FIGHTER_VISUAL_STATES = [
  'idle',
  'movement',
  'punch',
  'kick',
  'block',
  'hit',
  'knockout',
  'result',
] as const;

export type FighterVisualState = (typeof FIGHTER_VISUAL_STATES)[number];

const PLAYER_ID = 'visual-player';

function player(
  overrides: Partial<PlayerState> & Pick<PlayerState, 'player_id' | 'slot' | 'display_name' | 'character_id'>,
): PlayerState {
  return {
    health: 100,
    combo: { active: false, hits: 0, total_damage: 0 },
    connected: true,
    position_x: overrides.slot === 'player-1' ? 270 : 730,
    position_y: 0,
    facing: overrides.slot === 'player-1' ? 'right' : 'left',
    animation_state: 'idle',
    blocking: false,
    ...overrides,
  };
}

export function getFighterVisualFixture(name: string | null): {
  credentials: { matchId: string; playerId: string; sessionToken: string };
  state: MatchState;
} {
  const visualState = FIGHTER_VISUAL_STATES.includes(name as FighterVisualState)
    ? (name as FighterVisualState)
    : 'idle';

  const p1 = player({
    player_id: PLAYER_ID,
    slot: 'player-1',
    display_name: 'NOVA LLAMA',
    character_id: 'quantum-llama',
  });
  const p2 = player({
    player_id: 'visual-rival',
    slot: 'player-2',
    display_name: 'NEON PUMA',
    character_id: 'neon-puma',
    is_computer: true,
  });

  if (visualState === 'movement') {
    p1.animation_state = 'move_right';
    p1.position_x = 380;
  } else if (visualState === 'punch') {
    p1.animation_state = 'light_punch';
  } else if (visualState === 'kick') {
    p1.animation_state = 'heavy_kick';
  } else if (visualState === 'block') {
    p1.animation_state = 'block';
    p1.blocking = true;
  } else if (visualState === 'hit') {
    p1.animation_state = 'hit';
    p1.health = 64;
  } else if (visualState === 'knockout' || visualState === 'result') {
    p1.animation_state = 'knockout';
    p1.health = 0;
  }

  const isResult = visualState === 'result';
  return {
    credentials: {
      matchId: 'visual-match',
      playerId: PLAYER_ID,
      sessionToken: 'visual-session',
    },
    state: {
      match_id: 'visual-match',
      status: isResult ? 'round_complete' : 'active',
      round_seconds_remaining: isResult ? 0 : 73,
      winner_slot: isResult ? p2.slot : null,
      revision: 1,
      entry_validation: {
        target_usd: '0.05',
        amount_tlama: '125.00',
      },
      players: {
        [p1.player_id]: p1,
        [p2.player_id]: p2,
      },
    },
  };
}