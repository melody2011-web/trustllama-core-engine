import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './tests/fighting-game',
  snapshotPathTemplate: '{testDir}/__screenshots__/{projectName}/{arg}{ext}',
  fullyParallel: false,
  workers: 1,
  reporter: 'line',
  use: {
    baseURL: 'http://127.0.0.1:4177',
    colorScheme: 'dark',
    launchOptions: {
      executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH
        ?? '/repl/tools/bin/chromium',
    },
    locale: 'en-US',
    reducedMotion: 'reduce',
    screenshot: 'only-on-failure',
  },
  expect: {
    toHaveScreenshot: {
      animations: 'disabled',
      caret: 'hide',
      maxDiffPixelRatio: 0.01,
    },
  },
  projects: [
    {
      name: 'desktop',
      use: { viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 },
    },
    {
      name: 'phone',
      use: {
        ...devices['iPhone 13'],
        browserName: 'chromium',
        viewport: { width: 390, height: 844 },
      },
    },
  ],
  webServer: {
    command: 'pnpm exec vite --config vite.config.ts --mode visual-test --host 127.0.0.1 --port 4177',
    url: 'http://127.0.0.1:4177/llama-website/fighting-game?visualState=idle',
    reuseExistingServer: false,
    timeout: 120_000,
  },
});