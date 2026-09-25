import path from 'path';
import { timingSafeEqual } from 'node:crypto';
import { createReadStream, statSync } from 'node:fs';
import type { IncomingMessage, ServerResponse } from 'node:http';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import { defineConfig, type Plugin } from 'vite';

import runtimeErrorOverlay from '@replit/vite-plugin-runtime-error-modal';
import { sendApprovedWebhookArchive } from './operator-webhook-download';

const rawPort = process.env.PORT ?? '5173';

const port = Number(rawPort);

if (Number.isNaN(port) || port <= 0) {
  throw new Error(`Invalid PORT value: "${rawPort}"`);
}

const basePath = process.env.BASE_PATH ?? '/llama-website/';
const backendReleaseFilename = 'trustllama-core-backend-v44.zip';
const backendReleasePath = path.resolve(
  import.meta.dirname,
  '..',
  '..',
  backendReleaseFilename,
);
const webhookReleaseFilename = 'solana-webhook-core-rail-pro.zip';
const webhookReleasePath = path.resolve(
  import.meta.dirname,
  '..',
  '..',
  'solana-webhook-core-rail-pro',
  webhookReleaseFilename,
);

function authorizeLocalDownload(request: IncomingMessage, response: ServerResponse): boolean {
  response.setHeader('Cache-Control', 'no-store, private');
  response.setHeader('X-Content-Type-Options', 'nosniff');
  const password = process.env.OPERATOR_DOWNLOAD_PASSWORD;
  if (!password || password.length < 16) {
    response.statusCode = 503;
    response.end('Operator downloads are unavailable.');
    return false;
  }

  const authorization = request.headers.authorization;
  const encoded = authorization?.match(/^Basic ([A-Za-z0-9+/]+={0,2})$/)?.[1];
  const credentials = encoded ? Buffer.from(encoded, 'base64').toString('utf8') : '';
  const actual = Buffer.from(credentials);
  const expected = Buffer.from(`operator:${password}`);
  if (actual.length !== expected.length || !timingSafeEqual(actual, expected)) {
    response.statusCode = 401;
    response.setHeader('WWW-Authenticate', 'Basic realm="TrustLlama operator downloads", charset="UTF-8"');
    response.end('Operator authorization required.');
    return false;
  }
  return true;
}

function localBackendReleaseDownload(): Plugin {
  return {
    name: 'local-backend-release-download',
    apply: 'serve',
    configureServer(server) {
      server.middlewares.use((request, response, next) => {
        const requestPath = request.url?.split('?')[0];
        if (
          requestPath !== `/${backendReleaseFilename}`
        ) {
          next();
          return;
        }

        if (!authorizeLocalDownload(request, response)) return;
        if (request.method !== 'GET') {
          response.statusCode = 405;
          response.setHeader('Allow', 'GET');
          response.end();
          return;
        }
        try {
          const archive = statSync(backendReleasePath);
          response.statusCode = 200;
          response.setHeader('Content-Type', 'application/zip');
          response.setHeader('Content-Length', archive.size);
          response.setHeader(
            'Content-Disposition',
            `attachment; filename="${backendReleaseFilename}"`,
          );
          createReadStream(backendReleasePath).pipe(response);
        } catch {
          response.statusCode = 404;
          response.end('Backend release archive is unavailable.');
        }
      });
    },
  };
}

function localWebhookProDownload(): Plugin {
  return {
    name: 'local-webhook-pro-download',
    apply: 'serve',
    configureServer(server) {
      server.middlewares.use((request, response, next) => {
        if (
          request.url?.split('?')[0] !== `/${webhookReleaseFilename}`
        ) {
          next();
          return;
        }

        if (!authorizeLocalDownload(request, response)) return;
        if (request.method !== 'GET') {
          response.statusCode = 405;
          response.setHeader('Allow', 'GET');
          response.end();
          return;
        }
        sendApprovedWebhookArchive(response, webhookReleasePath, webhookReleaseFilename);
      });
    },
  };
}

export default defineConfig({
  base: basePath,
  plugins: [
    localBackendReleaseDownload(),
    localWebhookProDownload(),
    react(),
    tailwindcss(),
    runtimeErrorOverlay(),
    ...(process.env.NODE_ENV !== 'production' &&
    process.env.REPL_ID !== undefined
      ? [
          await import('@replit/vite-plugin-cartographer').then((m) =>
            m.cartographer({
              root: path.resolve(import.meta.dirname, '..'),
            }),
          ),
        ]
      : []),
  ],
  resolve: {
    alias: {
      '@': path.resolve(import.meta.dirname, 'src'),
      '@assets': path.resolve(
        import.meta.dirname,
        '..',
        '..',
        'attached_assets',
      ),
    },
    dedupe: ['react', 'react-dom'],
  },
  root: path.resolve(import.meta.dirname),
  build: {
    outDir: path.resolve(import.meta.dirname, 'dist/public'),
    emptyOutDir: true,
  },
  server: {
    port,
    strictPort: true,
    host: '0.0.0.0',
    allowedHosts: true,
    fs: {
      strict: true,
      // Vite otherwise permits /@fs/ requests inside the workspace root.
      deny: [backendReleaseFilename, webhookReleaseFilename, 'approved-release.json'],
    },
  },
  preview: {
    port,
    host: '0.0.0.0',
    allowedHosts: true,
  },
});
