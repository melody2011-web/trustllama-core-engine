export const GAME2_SANDBOX_BASE_PATH = '/retro-arcade/game2-sandbox';
export const PRODUCTION_ARCADE_BASE_PATH = '/llama-website/game';

const GAME2_ENVIRONMENTS = {
  development: {
    basePath: GAME2_SANDBOX_BASE_PATH,
    playerStorageKey: 'tlama-game2-sandbox-player-key',
    ownershipStorageKey: 'tlama-game2-sandbox-ownership-token',
    readyMessage: 'Connected to Dev-Sandbox (Testing Active)',
    developerControlsEnabled: true,
  },
  production: {
    basePath: PRODUCTION_ARCADE_BASE_PATH,
    playerStorageKey: 'tlama-arcade-player-key',
    ownershipStorageKey: 'tlama-arcade-ownership-token',
    readyMessage: 'Ready for server connection',
    developerControlsEnabled: false,
  },
} as const;

type Game2Mode = keyof typeof GAME2_ENVIRONMENTS;

function selectGame2Environment(mode: string | undefined) {
  if (mode === 'development' || mode === 'production') {
    return GAME2_ENVIRONMENTS[mode satisfies Game2Mode];
  }

  throw new Error(
    `Invalid VITE_GAME2_MODE: ${JSON.stringify(mode)}. Expected "development" or "production".`,
  );
}

export const GAME2_ENVIRONMENT = selectGame2Environment(
  import.meta.env.VITE_GAME2_MODE,
);

export const GAME2_BASE_PATH = GAME2_ENVIRONMENT.basePath;
export const GAME2_WEBSOCKET_PATH = `${GAME2_BASE_PATH}/ws`;
export const GAME2_PAYMENT_CONFIG_PATH = `${GAME2_BASE_PATH}/payment-config`;