import { spawn } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const artifactDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const workspaceDir = path.resolve(artifactDir, '..', '..');
const sandboxBasePath = '/retro-arcade/game2-sandbox';

export function sandboxEnvironment(sourceEnvironment = process.env) {
  const env = { ...sourceEnvironment };
  const sandboxDatabase = path.resolve(
    env.GAME2_SANDBOX_DB_PATH ?? '/tmp/trustllama-game2-sandbox.sqlite3',
  );
  const layerDatabase = env.TLAMA_GAME_DB_PATH
    ? path.resolve(env.TLAMA_GAME_DB_PATH)
    : '';
  if (layerDatabase && sandboxDatabase === layerDatabase) {
    throw new Error('Game 2 sandbox database must be isolated from the Layer 1 database.');
  }
  for (const key of Object.keys(env)) {
    if (/(?:PRIVATE_KEY|WALLET|RPC_URL|BOT_TOKEN|SESSION_SECRET|PASSWORD)/i.test(key)) {
      delete env[key];
    }
  }
  return {
    ...env,
    PORT: env.GAME2_SANDBOX_PORT ?? '22369',
    TLAMA_GAME_PORT: env.GAME2_SANDBOX_PORT ?? '22369',
    TLAMA_GAME_HOST: '127.0.0.1',
    BASE_PATH: sandboxBasePath,
    TLAMA_GAME_DB_PATH: sandboxDatabase,
    TLAMA_PAYMENT_ENABLED: 'false',
    TLAMA_ARCADE_ADMIN_PORTAL_ENABLED: 'false',
    PYTHONUNBUFFERED: '1',
  };
}

export function startSandbox() {
  const backendEnvironment = sandboxEnvironment();
  const backendListenFd = Number.parseInt(
    process.env.GAME2_SANDBOX_LISTEN_FD ?? '',
    10,
  );
  const viteListenFd = Number.parseInt(process.env.GAME2_VITE_LISTEN_FD ?? '', 10);
  const hasBackendListenFd = Number.isInteger(backendListenFd) && backendListenFd >= 0;
  const hasViteListenFd = Number.isInteger(viteListenFd) && viteListenFd >= 0;
  const backend = spawn(
    path.join(workspaceDir, '.pythonlibs', 'bin', 'python'),
    ['-u', path.join(workspaceDir, 'llama_website', 'llama_game', 'backend.py')],
    {
      cwd: workspaceDir,
      env: {
        ...backendEnvironment,
        ...(hasBackendListenFd ? { TLAMA_GAME_LISTEN_FD: '3' } : {}),
      },
      stdio: hasBackendListenFd
        ? ['inherit', 'inherit', 'inherit', backendListenFd]
        : 'inherit',
    },
  );

  const vite = spawn(
    'vite',
    ['--config', 'vite.config.ts', '--host', '0.0.0.0', '--strictPort'],
    {
      cwd: artifactDir,
      env: {
        ...process.env,
        GAME2_MODE: 'development',
        GAME2_SANDBOX_PORT: backendEnvironment.PORT,
        ...(hasViteListenFd ? { VITE_LISTEN_FD: '3' } : {}),
      },
      stdio: hasViteListenFd
        ? ['inherit', 'inherit', 'inherit', viteListenFd]
        : 'inherit',
    },
  );

  let stopping = false;

  function stop(signal = 'SIGTERM') {
    if (stopping) return;
    stopping = true;
    backend.kill(signal);
    vite.kill(signal);
  }

  backend.on('exit', (code, signal) => {
    if (!stopping) {
      console.error(`Game 2 sandbox backend stopped (${signal ?? code ?? 'unknown'}).`);
      stop();
      process.exitCode = code ?? 1;
    }
  });

  vite.on('exit', (code, signal) => {
    if (!stopping) {
      console.error(`Game 2 frontend stopped (${signal ?? code ?? 'unknown'}).`);
      stop();
      process.exitCode = code ?? 1;
    }
  });

  for (const signal of ['SIGINT', 'SIGTERM']) {
    process.on(signal, () => stop(signal));
  }
}

if (
  process.argv[1]
  && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href
) {
  startSandbox();
}
