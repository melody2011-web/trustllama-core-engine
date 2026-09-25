import {
  createElement,
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import { applyLevelOneStep } from '@/game/level-one-rules';
import {
  GAME2_ENVIRONMENT,
  GAME2_PAYMENT_CONFIG_PATH,
  GAME2_WEBSOCKET_PATH,
} from '@/lib/game2-routes';

export type ConnectionStatus = 'disconnected' | 'connecting' | 'connected' | 'error';
export type TicketStatus = 'none' | 'pending' | 'valid' | 'used' | 'error';

export interface ArcadeConnection {
  status: ConnectionStatus;
  message: string;
  playerId?: string;
  ticketId?: string;
  ticketStatus: TicketStatus;
  entryFeeTlama: number;
  paymentsEnabled: boolean;
}

export interface ArcadePlayer {
  player_id: string;
  name: string;
  x: number;
  y: number;
  score: number;
  current_level: number;
  lives: number;
  max_lives?: number;
  game_over: boolean;
  victory_announced?: boolean;
  equipped_skin_id?: string;
  extra_lives_purchased?: number;
  extra_life_unlocked?: boolean;
  oxygen?: number;
}

export interface ArcadeCollectible {
  id: string;
  x: number;
  y: number;
  type?: string;
}

export interface ArcadeHazard {
  id: string;
  kind: string;
  active: boolean;
  x: number;
  y: number;
  radius: number;
}

export interface ArcadeLevel {
  number: number;
  name: string;
  min_score: number;
  max_score: number | null;
  next_score: number;
}

export interface ArcadeCatalogItem {
  id: string;
  label: string;
  amount_tlama: number;
  description: string;
  unlock_level?: number;
  max_per_match?: number;
}

export interface LevelOneItem {
  id: string;
  x: number;
  y: number;
}

export interface LevelOneHazard {
  id: string;
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface LevelOneRun {
  items: LevelOneItem[];
  hazards: LevelOneHazard[];
  collectedIds: string[];
  finishX: number;
  completed: boolean;
}

export interface ArcadeState {
  players: ArcadePlayer[];
  collectibles: Record<string, ArcadeCollectible>;
  levels: ArcadeLevel[];
  level7_worlds: Record<string, unknown>;
  level8_worlds: Record<string, unknown>;
  level9_worlds: Record<string, unknown>;
  level10_worlds: Record<string, unknown>;
  starting_lives: number;
  maximum_lives: number;
  extra_life_catalog: ArcadeCatalogItem[];
  simulated_entry_fee_tlama: number;
  levelOne: LevelOneRun;
  logs: ArcadeHazard[];
}

export interface ArcadeActions {
  join: (name: string) => void;
  move: (x: number, y: number) => void;
  reset: () => void;
  purchaseHeartUpgrade: () => void;
  primaryAction: (pressed: boolean) => void;
  directionalAction: (axis: { x: number; y: number }, jump?: boolean) => void;
  joinTestLevel: (name: string, level: 1 | 2 | 3, password: string) => Promise<void>;
}

interface EngineValue {
  connection: ArcadeConnection;
  player?: ArcadePlayer;
  state: ArcadeState;
  actions: ArcadeActions;
}

interface PaymentConfig {
  enabled?: boolean;
  catalog?: ArcadeCatalogItem[];
}

const EMPTY_STATE: ArcadeState = {
  players: [],
  collectibles: {},
  levels: [],
  level7_worlds: {},
  level8_worlds: {},
  level9_worlds: {},
  level10_worlds: {},
  starting_lives: 3,
  maximum_lives: 5,
  extra_life_catalog: [],
  simulated_entry_fee_tlama: 25,
  levelOne: {
    items: [
      { id: 'neon-chip-1', x: 0.14, y: 0.62 },
      { id: 'neon-chip-2', x: 0.31, y: 0.34 },
      { id: 'neon-chip-3', x: 0.5, y: 0.67 },
      { id: 'neon-chip-4', x: 0.69, y: 0.3 },
      { id: 'neon-chip-5', x: 0.84, y: 0.58 },
    ],
    hazards: [
      { id: 'laser-1', x: 0.22, y: 0.58, width: 0.035, height: 0.28 },
      { id: 'laser-2', x: 0.41, y: 0.12, width: 0.035, height: 0.3 },
      { id: 'laser-3', x: 0.59, y: 0.55, width: 0.04, height: 0.31 },
      { id: 'laser-4', x: 0.77, y: 0.15, width: 0.035, height: 0.3 },
    ],
    collectedIds: [],
    finishX: 0.94,
    completed: false,
  },
  logs: [],
};

const EngineContext = createContext<EngineValue | null>(null);
const PLAYER_KEY = GAME2_ENVIRONMENT.playerStorageKey;
const OWNERSHIP_KEY = GAME2_ENVIRONMENT.ownershipStorageKey;
const READY_MESSAGE = GAME2_ENVIRONMENT.readyMessage;

function cookieValue(name: string) {
  const prefix = `${name}=`;
  return document.cookie
    .split(';')
    .map((item) => item.trim())
    .find((item) => item.startsWith(prefix))
    ?.slice(prefix.length) ?? '';
}

function createPlayerKey() {
  return crypto.randomUUID();
}

function createOwnershipToken() {
  const bytes = crypto.getRandomValues(new Uint8Array(32));
  let binary = '';
  bytes.forEach((byte) => {
    binary += String.fromCharCode(byte);
  });
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

function persistentIdentity() {
  const readOrCreate = (key: string, create: () => string) => {
    try {
      const saved = localStorage.getItem(key);
      if (saved) return saved;
      const value = create();
      localStorage.setItem(key, value);
      return value;
    } catch {
      return create();
    }
  };
  return {
    playerKey: readOrCreate(PLAYER_KEY, createPlayerKey),
    ownershipToken: readOrCreate(OWNERSHIP_KEY, createOwnershipToken),
  };
}

function websocketUrl() {
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${protocol}//${location.host}${GAME2_WEBSOCKET_PATH}`;
}

function normalizeCollectibles(value: unknown): Record<string, ArcadeCollectible> {
  if (!value || typeof value !== 'object') return {};
  return Object.fromEntries(
    Object.entries(value as Record<string, Omit<ArcadeCollectible, 'id'>>).map(
      ([id, collectible]) => [id, { id, ...collectible }],
    ),
  );
}

export function ArcadeEngineProvider({ children }: { children: ReactNode }) {
  const socketRef = useRef<WebSocket | null>(null);
  const playerIdRef = useRef<string | undefined>(undefined);
  const levelRef = useRef(1);
  const playerRef = useRef<ArcadePlayer | undefined>(undefined);
  const sequenceRef = useRef(0);
  const chargeHeldRef = useRef(false);
  const lastCollectedRef = useRef('');
  const pendingTestLevelRef = useRef<1 | 2 | 3 | null>(null);
  const levelOneRef = useRef<LevelOneRun>(EMPTY_STATE.levelOne);
  const [connection, setConnection] = useState<ArcadeConnection>({
    status: 'disconnected',
    message: READY_MESSAGE,
    ticketStatus: 'none',
    entryFeeTlama: 25,
    paymentsEnabled: false,
  });
  const [state, setState] = useState<ArcadeState>(EMPTY_STATE);

  const resetLevelOne = useCallback(() => {
    const next = { ...EMPTY_STATE.levelOne, collectedIds: [], completed: false };
    levelOneRef.current = next;
    setState((current) => ({ ...current, levelOne: next }));
  }, []);

  const send = useCallback((message: Record<string, unknown>) => {
    if (socketRef.current?.readyState === WebSocket.OPEN) {
      socketRef.current.send(JSON.stringify(message));
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    fetch(GAME2_PAYMENT_CONFIG_PATH, { cache: 'no-store' })
      .then((response) => {
        if (!response.ok) throw new Error('Payment configuration unavailable');
        return response.json() as Promise<PaymentConfig>;
      })
      .then((config) => {
        if (cancelled) return;
        setConnection((current) => ({
          ...current,
          paymentsEnabled: config.enabled === true,
        }));
        if (Array.isArray(config.catalog)) {
          const upgrades = config.catalog.filter((item) => item.id === 'heart-matrix-upgrade');
          setState((current) => ({ ...current, extra_life_catalog: upgrades }));
        }
      })
      .catch(() => {
        if (!cancelled) {
          setConnection((current) => ({ ...current, paymentsEnabled: false }));
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const applySnapshot = useCallback((message: Record<string, unknown>) => {
    if (!Array.isArray(message.players)) return;
    const players = message.players as ArcadePlayer[];
    const local = players.find((candidate) => candidate.player_id === playerIdRef.current);
    if (local) {
      playerRef.current = local;
      levelRef.current = local.current_level;
    }
    setState((current) => ({
      ...current,
      players,
      collectibles: normalizeCollectibles(message.collectibles),
      logs: Array.isArray(message.logs) ? (message.logs as ArcadeHazard[]) : current.logs,
      levels: Array.isArray(message.levels) ? (message.levels as ArcadeLevel[]) : current.levels,
      level7_worlds: (message.level7_worlds as Record<string, unknown>) ?? {},
      level8_worlds: (message.level8_worlds as Record<string, unknown>) ?? {},
      level9_worlds: (message.level9_worlds as Record<string, unknown>) ?? {},
      level10_worlds: (message.level10_worlds as Record<string, unknown>) ?? {},
      starting_lives: Number(message.starting_lives ?? current.starting_lives),
      maximum_lives: Number(message.maximum_lives ?? current.maximum_lives),
      extra_life_catalog: Array.isArray(message.extra_life_catalog)
        ? (message.extra_life_catalog as ArcadeCatalogItem[])
        : current.extra_life_catalog,
      simulated_entry_fee_tlama: Number(
        message.simulated_entry_fee_tlama ?? current.simulated_entry_fee_tlama,
      ),
    }));

    if (local && local.current_level <= 6 && !local.game_over) {
      const collectibles = normalizeCollectibles(message.collectibles);
      const nearby = Object.values(collectibles).find(
        (orb) => Math.hypot(orb.x - local.x, orb.y - local.y) <= 0.065,
      );
      if (nearby && nearby.id !== lastCollectedRef.current) {
        lastCollectedRef.current = nearby.id;
        send({ type: 'collect', orb_id: nearby.id });
      } else if (!nearby) {
        lastCollectedRef.current = '';
      }
    }
  }, [send]);

  const join = useCallback((name: string) => {
    resetLevelOne();
    socketRef.current?.close(1000, 'Starting a new cabinet connection.');
    setConnection((current) => ({
      ...current,
      status: 'connecting',
      message: 'Connecting to authoritative Arcade engine…',
      ticketStatus: 'pending',
    }));
    const socket = new WebSocket(websocketUrl());
    socketRef.current = socket;
    socket.addEventListener('open', () => {
      const identity = persistentIdentity();
      socket.send(JSON.stringify({
        type: 'join',
        player_key: identity.playerKey,
        ownership_token: identity.ownershipToken,
        name,
      }));
    });
    socket.addEventListener('message', (event) => {
      let message: Record<string, unknown>;
      try {
        message = JSON.parse(String(event.data)) as Record<string, unknown>;
      } catch {
        return;
      }
      if (message.type === 'welcome') {
        const playerId = String(message.player_id ?? '');
        playerIdRef.current = playerId;
        setConnection((current) => ({
          ...current,
          status: 'connected',
          message: String(message.message ?? 'Connected'),
          playerId,
          ticketId: String(message.ticket_id ?? ''),
          ticketStatus: message.ticket_status === 'simulated_active' ? 'valid' : 'error',
          entryFeeTlama: Number(message.simulated_entry_fee_tlama ?? 25),
        }));
        const pendingTestLevel = pendingTestLevelRef.current;
        if (pendingTestLevel !== null) {
          pendingTestLevelRef.current = null;
          socket.send(JSON.stringify({
            type: 'admin_configure_test',
            level: pendingTestLevel,
            test_skin_id: '',
          }));
        }
      } else if (message.type === 'error') {
        setConnection((current) => ({
          ...current,
          status: current.status === 'connecting' ? 'error' : current.status,
          message: String(message.message ?? 'Arcade request rejected'),
        }));
      }
      if (message.players) applySnapshot(message);
    });
    socket.addEventListener('error', () => {
      setConnection((current) => ({
        ...current,
        status: 'error',
        ticketStatus: 'error',
        message: 'Authoritative Arcade engine unavailable',
      }));
    });
    socket.addEventListener('close', (event) => {
      if (socketRef.current !== socket) return;
      socketRef.current = null;
      playerIdRef.current = undefined;
      playerRef.current = undefined;
      setConnection((current) => ({
        ...current,
        status: event.code === 1000 ? 'disconnected' : 'error',
        ticketStatus: 'none',
        message: event.code === 1000 ? 'Cabinet disconnected' : 'Connection interrupted',
      }));
      setState((current) => ({ ...current, players: [] }));
    });
  }, [applySnapshot, resetLevelOne]);

  const joinTestLevel = useCallback(async (
    name: string,
    level: 1 | 2 | 3,
    password: string,
  ) => {
    if (!GAME2_ENVIRONMENT.developerControlsEnabled) {
      throw new Error('Developer test controls are unavailable in production.');
    }
    const statusResponse = await fetch('/admin/status', {
      credentials: 'same-origin',
      cache: 'no-store',
    });
    const status = await statusResponse.json() as { enabled?: boolean };
    if (!statusResponse.ok || status.enabled !== true) {
      throw new Error('The authoritative test portal is not enabled.');
    }

    const authResponse = await fetch('/admin/auth', {
      method: 'POST',
      credentials: 'same-origin',
      cache: 'no-store',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        password,
        csrf: cookieValue('tlama_arcade_admin_csrf'),
      }),
    });
    if (!authResponse.ok) {
      throw new Error('Developer authentication failed.');
    }

    pendingTestLevelRef.current = level;
    join(name);
  }, [join]);

  useEffect(() => () => socketRef.current?.close(1000, 'Retro Arcade unmounted.'), []);

  const move = useCallback((x: number, y: number) => {
    const local = playerRef.current;
    if (!local || local.current_level > 6 || local.game_over) return;
    send({
      type: 'position',
      x: Math.max(0, Math.min(1, x)),
      y: Math.max(0, Math.min(1, y)),
    });
  }, [send]);

  const reset = useCallback(() => {
    const local = playerRef.current;
    if (socketRef.current?.readyState === WebSocket.OPEN && local?.game_over) {
      resetLevelOne();
      send({ type: 'reset' });
      return;
    }
    socketRef.current?.close(1000, 'Player reset the cabinet.');
    socketRef.current = null;
    playerIdRef.current = undefined;
    playerRef.current = undefined;
    setConnection((current) => ({
      ...current,
      status: 'disconnected',
      message: READY_MESSAGE,
      ticketStatus: 'none',
      playerId: undefined,
      ticketId: undefined,
    }));
    setState((current) => ({ ...current, players: [] }));
    resetLevelOne();
  }, [resetLevelOne, send]);

  const directionalAction = useCallback((axis: { x: number; y: number }, jump = false) => {
    const local = playerRef.current;
    if (!local || local.game_over) return;
    const sequence = ++sequenceRef.current;
    if (local.current_level <= 6) {
      let nextX = Math.max(0, Math.min(1, local.x + axis.x * 0.022));
      let nextY = Math.max(0, Math.min(1, local.y + axis.y * 0.022));
      if (local.current_level === 1) {
        const run = levelOneRef.current;
        const step = applyLevelOneStep(run, local, { x: nextX, y: nextY });
        const newlyCollectedIds = step.run.collectedIds.filter(
          (chipId) => !run.collectedIds.includes(chipId),
        );
        const justCompleted = step.run.completed && !run.completed;
        nextX = step.position.x;
        nextY = step.position.y;
        if (
          step.run.collectedIds.length !== run.collectedIds.length
          || step.run.completed !== run.completed
        ) {
          levelOneRef.current = step.run;
          setState((current) => ({ ...current, levelOne: step.run }));
        }
        move(nextX, nextY);
        newlyCollectedIds.forEach((chipId) => {
          send({ type: 'level_one_chip', chip_id: chipId });
        });
        if (justCompleted) {
          send({ type: 'level_one_complete' });
        }
        return;
      }
      move(nextX, nextY);
    } else if (local.current_level === 7) {
      send({ type: 'vehicle_input', thrust: axis.y < 0, fire: false, sequence });
    } else if (local.current_level === 8) {
      send({ type: 'submarine_input', thrust: axis.y > 0, sequence });
    } else if (local.current_level === 9) {
      send({
        type: 'platformer_input',
        run_axis: Math.sign(axis.x),
        jump_pressed: jump || axis.y < 0,
        sequence,
      });
    } else {
      send({
        type: 'citadel_input',
        booster_x: Math.sign(axis.x),
        booster_y: Math.sign(axis.y),
        charge_pressed: chargeHeldRef.current,
        fire_released: false,
        sequence,
      });
    }
  }, [move, send]);

  const primaryAction = useCallback((pressed: boolean) => {
    const local = playerRef.current;
    if (!local || local.game_over) return;
    const sequence = ++sequenceRef.current;
    if (local.current_level === 2 && pressed) {
      move(local.x, local.y - 0.075);
    } else if (local.current_level === 7 && pressed) {
      send({ type: 'vehicle_input', thrust: false, fire: true, sequence });
    } else if (local.current_level === 9 && pressed) {
      send({ type: 'platformer_input', run_axis: 0, jump_pressed: true, sequence });
    } else if (local.current_level === 10) {
      const wasHeld = chargeHeldRef.current;
      chargeHeldRef.current = pressed;
      send({
        type: 'citadel_input',
        booster_x: 0,
        booster_y: 0,
        charge_pressed: pressed,
        fire_released: wasHeld && !pressed,
        sequence,
      });
    }
  }, [move, send]);

  const purchaseHeartUpgrade = useCallback(() => {
    setConnection((current) => ({
      ...current,
      message: current.paymentsEnabled
        ? 'Connect a supported wallet in Module 1 to produce the required verified settlement.'
        : 'Heart Matrix settlement is currently disabled by the authoritative payment service.',
    }));
  }, []);

  const player = state.players.find((candidate) => candidate.player_id === connection.playerId);
  const actions = useMemo<ArcadeActions>(() => ({
    join,
    move,
    reset,
    purchaseHeartUpgrade,
    primaryAction,
    directionalAction,
    joinTestLevel,
  }), [directionalAction, join, joinTestLevel, move, primaryAction, purchaseHeartUpgrade, reset]);

  return createElement(
    EngineContext.Provider,
    { value: { connection, player, state, actions } },
    children,
  );
}

export function useArcadeEngine() {
  const value = useContext(EngineContext);
  if (!value) throw new Error('useArcadeEngine must be used inside ArcadeEngineProvider');
  return value;
}