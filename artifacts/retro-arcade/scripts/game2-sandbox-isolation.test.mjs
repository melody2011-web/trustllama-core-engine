import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createHash, randomUUID } from 'node:crypto';
import {
  existsSync,
  mkdtempSync,
  readFileSync,
  rmSync,
} from 'node:fs';
import { createServer } from 'node:net';
import { tmpdir } from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import WebSocket from 'ws';

import { sandboxEnvironment } from './game2-sandbox-dev.mjs';

const artifactDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const workspaceDir = path.resolve(artifactDir, '..', '..');
const python = path.join(workspaceDir, '.pythonlibs', 'bin', 'python');
const backend = path.join(workspaceDir, 'llama_website', 'llama_game', 'backend.py');
function reserveLoopbackPort() {
  const server = createServer();
  return new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const address = server.address();
      resolve({
        port: address.port,
        fd: server._handle.fd,
        close: () => server.close(),
      });
    });
  });
}

function start(command, args, options) {
  const child = spawn(command, args, {
    ...options,
    stdio: options.stdio ?? ['ignore', 'pipe', 'pipe'],
  });
  let output = '';
  child.stdout.on('data', (chunk) => { output += chunk; });
  child.stderr.on('data', (chunk) => { output += chunk; });
  child.output = () => output;
  return child;
}

async function waitForHealth(url, child) {
  const deadline = Date.now() + 15_000;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) {
      throw new Error(`Process stopped before becoming healthy:\n${child.output()}`);
    }
    try {
      const response = await fetch(url);
      if (response.ok) return response.json();
    } catch {}
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(`Timed out waiting for ${url}:\n${child.output()}`);
}

function databaseDigest(databasePath) {
  const digest = createHash('sha256');
  for (const suffix of ['', '-wal', '-shm']) {
    const file = `${databasePath}${suffix}`;
    digest.update(suffix);
    if (existsSync(file)) digest.update(readFileSync(file));
  }
  return digest.digest('hex');
}

function receiveUntil(socket, label, predicate) {
  return new Promise((resolve, reject) => {
    const seen = [];
    const timeout = setTimeout(
      () => reject(new Error(`Timed out waiting for ${label}; saw: ${seen.join(', ')}`)),
      5_000,
    );
    const onMessage = (data) => {
      const message = JSON.parse(data.toString());
      seen.push(`${message.type ?? 'unknown'}:${message.event ?? ''}`);
      if (!predicate(message)) return;
      clearTimeout(timeout);
      socket.off('message', onMessage);
      resolve(message);
    };
    socket.on('message', onMessage);
  });
}

test('Game 2 sandbox cannot alter Layer 1 data or enable privileged modes', async (t) => {
  const directory = mkdtempSync(path.join(tmpdir(), 'game2-isolation-'));
  t.after(() => {
    rmSync(directory, { recursive: true, force: true });
  });
  const [layerReservation, sandboxReservation, viteReservation] = await Promise.all([
    reserveLoopbackPort(),
    reserveLoopbackPort(),
    reserveLoopbackPort(),
  ]);
  const layerDatabase = path.join(directory, 'layer1.sqlite3');
  const sandboxDatabase = path.join(directory, 'game2.sqlite3');
  const layerPort = layerReservation.port;
  const sandboxPort = sandboxReservation.port;
  const vitePort = viteReservation.port;
  const secrets = {
    WALLET_PRIVATE_KEY: 'must-not-reach-game2',
    BSC_RPC_URL: 'must-not-reach-game2',
    TELEGRAM_BOT_TOKEN: 'must-not-reach-game2',
    SESSION_SECRET: 'must-not-reach-game2',
    TLAMA_ARCADE_ADMIN_PASSWORD: 'must-not-reach-game2',
  };
  const safeEnvironment = sandboxEnvironment({
    ...process.env,
    ...secrets,
    TLAMA_GAME_DB_PATH: layerDatabase,
    GAME2_SANDBOX_PORT: String(sandboxPort),
    GAME2_SANDBOX_DB_PATH: sandboxDatabase,
  });

  for (const key of Object.keys(secrets)) {
    assert.equal(safeEnvironment[key], undefined, `${key} leaked into the sandbox`);
  }
  assert.equal(safeEnvironment.TLAMA_PAYMENT_ENABLED, 'false');
  assert.equal(safeEnvironment.TLAMA_ARCADE_ADMIN_PORTAL_ENABLED, 'false');
  assert.throws(
    () => sandboxEnvironment({
      TLAMA_GAME_DB_PATH: layerDatabase,
      GAME2_SANDBOX_DB_PATH: layerDatabase,
    }),
    /must be isolated/,
  );

  const layer = start(python, ['-u', backend], {
    cwd: workspaceDir,
    env: {
      ...process.env,
      PORT: String(layerPort),
      TLAMA_GAME_HOST: '127.0.0.1',
      TLAMA_GAME_DB_PATH: layerDatabase,
      TLAMA_PAYMENT_ENABLED: 'false',
      TLAMA_ARCADE_ADMIN_PORTAL_ENABLED: 'false',
      TLAMA_GAME_LISTEN_FD: '3',
    },
    stdio: ['ignore', 'pipe', 'pipe', layerReservation.fd],
  });
  const game2 = start('node', ['scripts/game2-sandbox-dev.mjs'], {
    cwd: artifactDir,
    env: {
      ...process.env,
      ...secrets,
      PORT: String(vitePort),
      BASE_PATH: '/retro-arcade/',
      TLAMA_GAME_DB_PATH: layerDatabase,
      GAME2_SANDBOX_PORT: String(sandboxPort),
      GAME2_SANDBOX_DB_PATH: sandboxDatabase,
      GAME2_SANDBOX_LISTEN_FD: '3',
      GAME2_VITE_LISTEN_FD: '4',
    },
    stdio: ['ignore', 'pipe', 'pipe', sandboxReservation.fd, viteReservation.fd],
  });
  layerReservation.close();
  sandboxReservation.close();
  viteReservation.close();
  t.after(() => {
    game2.kill('SIGTERM');
    layer.kill('SIGTERM');
  });

  await Promise.all([
    waitForHealth(`http://127.0.0.1:${layerPort}/healthz`, layer),
    waitForHealth(
      `http://127.0.0.1:${vitePort}/retro-arcade/game2-sandbox/healthz`,
      game2,
    ),
  ]);
  const layerDigestBefore = databaseDigest(layerDatabase);
  const sandboxDigestBefore = databaseDigest(sandboxDatabase);

  const payment = await fetch(
    `http://127.0.0.1:${vitePort}/retro-arcade/game2-sandbox/payment-config`,
  ).then((response) => response.json());
  const admin = await fetch(
    `http://127.0.0.1:${vitePort}/retro-arcade/game2-sandbox/admin/status`,
  ).then((response) => response.json());
  assert.equal(payment.enabled, false);
  assert.equal(admin.enabled, false);

  const socket = new WebSocket(
    `ws://127.0.0.1:${vitePort}/retro-arcade/game2-sandbox/ws`,
    { origin: `http://127.0.0.1:${sandboxPort}` },
  );
  t.after(() => socket.close());
  await new Promise((resolve, reject) => {
    socket.once('open', resolve);
    socket.once('error', reject);
  });
  const welcomeMessage = receiveUntil(
    socket,
    'sandbox welcome',
    (message) => message.type === 'welcome',
  );
  socket.send(JSON.stringify({
    type: 'join',
    player_key: randomUUID(),
    ownership_token: `${randomUUID()}${randomUUID()}`,
    name: 'Isolation Llama',
  }));
  const welcome = await welcomeMessage;
  const movedState = receiveUntil(
    socket,
    'mutated player state',
    (message) => message.players?.some(
      (player) => player.player_id === welcome.player_id
        && (player.x !== 0.5 || player.y !== 0.5),
    ),
  );
  socket.send(JSON.stringify({ type: 'position', x: 0.51, y: 0.49 }));
  await movedState;

  const [layerHealth, sandboxHealth] = await Promise.all([
    fetch(`http://127.0.0.1:${layerPort}/healthz`).then((response) => response.json()),
    fetch(`http://127.0.0.1:${vitePort}/retro-arcade/game2-sandbox/healthz`)
      .then((response) => response.json()),
  ]);
  assert.equal(layerHealth.active_players, 0);
  assert.equal(sandboxHealth.active_players, 1);
  assert.deepEqual(layerHealth.storage, {
    saved_players: 0,
    saved_match_tickets: 0,
  });
  assert.deepEqual(sandboxHealth.storage, {
    saved_players: 1,
    saved_match_tickets: 1,
  });
  assert.equal(databaseDigest(layerDatabase), layerDigestBefore);
  assert.notEqual(databaseDigest(sandboxDatabase), sandboxDigestBefore);
});