import path from 'path';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import { defineConfig } from 'vite';

import runtimeErrorOverlay from '@replit/vite-plugin-runtime-error-modal';

const game2Mode = process.env.GAME2_MODE;

if (game2Mode !== 'development' && game2Mode !== 'production') {
  throw new Error(
    `Invalid GAME2_MODE: ${JSON.stringify(game2Mode)}. Expected "development" or "production".`,
  );
}

const rawPort = process.env.PORT;

if (!rawPort) {
  throw new Error(
    'PORT environment variable is required but was not provided.',
  );
}

const port = Number(rawPort);

if (Number.isNaN(port) || port <= 0) {
  throw new Error(`Invalid PORT value: "${rawPort}"`);
}

const basePath = process.env.BASE_PATH;

if (!basePath) {
  throw new Error(
    'BASE_PATH environment variable is required but was not provided.',
  );
}

const game2SandboxPort = Number(process.env.GAME2_SANDBOX_PORT ?? '22369');
const viteListenFd = Number.parseInt(process.env.VITE_LISTEN_FD ?? '', 10);

if (Number.isNaN(game2SandboxPort) || game2SandboxPort <= 0) {
  throw new Error(`Invalid GAME2_SANDBOX_PORT value: "${process.env.GAME2_SANDBOX_PORT}"`);
}

export default defineConfig({
  base: basePath,
  define: {
    'import.meta.env.VITE_GAME2_MODE': JSON.stringify(game2Mode),
  },
  plugins: [
    ...(Number.isInteger(viteListenFd) && viteListenFd >= 0
      ? [{
          name: 'inherited-listener',
          configureServer(server) {
            const httpServer = server.httpServer;
            if (!httpServer) throw new Error('Vite HTTP server is unavailable.');
            const listen = httpServer.listen.bind(httpServer);
            httpServer.listen = ((...args: Parameters<typeof httpServer.listen>) => {
              const callback = args.findLast(
                (argument): argument is () => void => typeof argument === 'function',
              );
              return listen({ fd: viteListenFd }, callback);
            }) as typeof httpServer.listen;
          },
        }]
      : []),
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
    proxy: {
      '/retro-arcade/game2-sandbox': {
        target: `http://127.0.0.1:${game2SandboxPort}`,
        changeOrigin: false,
        ws: true,
      },
    },
    fs: {
      strict: true,
    },
  },
  preview: {
    port,
    host: '0.0.0.0',
    allowedHosts: true,
  },
});
