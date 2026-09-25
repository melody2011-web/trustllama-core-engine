import path from 'path';
import { defineConfig } from 'vitest/config';

const game2Mode = process.env.GAME2_MODE;

if (game2Mode !== 'development' && game2Mode !== 'production') {
  throw new Error(
    `Invalid GAME2_MODE: ${JSON.stringify(game2Mode)}. Expected "development" or "production".`,
  );
}

export default defineConfig({
  define: {
    'import.meta.env.VITE_GAME2_MODE': JSON.stringify(game2Mode),
  },
  resolve: {
    alias: {
      '@': path.resolve(import.meta.dirname, 'src'),
    },
  },
  test: {
    environment: 'node',
    include: ['src/**/*.test.ts'],
  },
});