import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { pathToFileURL } from 'node:url';
import { build, createServer, loadConfigFromFile } from 'vite';

const packageRoot = path.resolve(import.meta.dirname, '..');
const authoritativeBasePath = '/llama-website/game';
const sandboxBasePath = '/retro-arcade/game2-sandbox';
const sandboxEnvironment = {
  basePath: sandboxBasePath,
  playerStorageKey: 'tlama-game2-sandbox-player-key',
  ownershipStorageKey: 'tlama-game2-sandbox-ownership-token',
  readyMessage: 'Connected to Dev-Sandbox (Testing Active)',
  developerControlsEnabled: true,
};
const productionEnvironment = {
  basePath: authoritativeBasePath,
  playerStorageKey: 'tlama-arcade-player-key',
  ownershipStorageKey: 'tlama-arcade-ownership-token',
  readyMessage: 'Ready for server connection',
  developerControlsEnabled: false,
};

process.env.PORT ??= '4173';
process.env.BASE_PATH ??= '/retro-arcade/';

async function withGame2Mode(mode, callback) {
  const previousMode = process.env.GAME2_MODE;
  if (mode === undefined) {
    delete process.env.GAME2_MODE;
  } else {
    process.env.GAME2_MODE = mode;
  }

  try {
    return await callback();
  } finally {
    if (previousMode === undefined) {
      delete process.env.GAME2_MODE;
    } else {
      process.env.GAME2_MODE = previousMode;
    }
  }
}

test('Game 2 production and development clients target their routed services', async (t) => {
  const outDir = await mkdtemp(path.join(os.tmpdir(), 'retro-arcade-routes-'));
  t.after(() => rm(outDir, { recursive: true, force: true }));

  const server = await withGame2Mode('development', () =>
    createServer({
      configFile: path.join(packageRoot, 'vite.config.ts'),
      root: packageRoot,
      logLevel: 'silent',
      server: { middlewareMode: true },
    }),
  );
  const developmentModule = await server.transformRequest(
    '/src/lib/game2-routes.ts',
  );
  assert.ok(developmentModule);
  const developmentRoutes = await import(
    `data:text/javascript;base64,${Buffer.from(developmentModule.code).toString('base64')}`,
  );
  assert.equal(
    developmentRoutes.GAME2_WEBSOCKET_PATH,
    `${sandboxBasePath}/ws`,
  );
  assert.equal(
    developmentRoutes.GAME2_PAYMENT_CONFIG_PATH,
    `${sandboxBasePath}/payment-config`,
  );
  assert.deepEqual(
    developmentRoutes.GAME2_ENVIRONMENT,
    sandboxEnvironment,
  );
  await server.close();

  await withGame2Mode('production', () =>
    build({
      configFile: path.join(packageRoot, 'vite.config.ts'),
      root: packageRoot,
      logLevel: 'silent',
      build: {
        outDir,
        emptyOutDir: true,
        lib: {
          entry: path.join(packageRoot, 'src/lib/game2-routes.ts'),
          formats: ['es'],
          fileName: 'game2-routes',
        },
        minify: false,
      },
    }),
  );

  const productionRoutes = await import(
    pathToFileURL(path.join(outDir, 'game2-routes.js')).href
  );
  assert.equal(
    productionRoutes.GAME2_WEBSOCKET_PATH,
    `${authoritativeBasePath}/ws`,
  );
  assert.equal(
    productionRoutes.GAME2_PAYMENT_CONFIG_PATH,
    `${authoritativeBasePath}/payment-config`,
  );
  assert.deepEqual(
    productionRoutes.GAME2_ENVIRONMENT,
    productionEnvironment,
  );
});

test('Game 2 build configuration rejects missing and unknown modes', async () => {
  const configFile = path.join(packageRoot, 'vite.config.ts');

  for (const mode of [undefined, 'staging']) {
    await assert.rejects(
      withGame2Mode(mode, () =>
        loadConfigFromFile(
          { command: 'build', mode: 'production' },
          configFile,
          packageRoot,
          'silent',
        ),
      ),
      /Invalid GAME2_MODE/,
    );
  }
});

test('Game 2 consumers derive environment behavior from the shared configuration', async () => {
  const consumerPaths = [
    'src/hooks/use-arcade-engine.ts',
    'src/components/join-screen.tsx',
  ];

  for (const relativePath of consumerPaths) {
    const source = await readFile(path.join(packageRoot, relativePath), 'utf8');
    assert.doesNotMatch(source, /import\.meta\.env\.DEV/);
    assert.match(source, /GAME2_ENVIRONMENT/);
  }
});

test('Game 2 routes use only the dedicated mode contract', async () => {
  const source = await readFile(
    path.join(packageRoot, 'src/lib/game2-routes.ts'),
    'utf8',
  );
  assert.doesNotMatch(source, /import\.meta\.env\.DEV/);
  assert.match(source, /import\.meta\.env\.VITE_GAME2_MODE/);
});