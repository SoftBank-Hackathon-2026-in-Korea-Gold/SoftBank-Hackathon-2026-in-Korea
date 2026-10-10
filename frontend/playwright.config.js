import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './e2e',
  testMatch: '**/*.pw.js',
  fullyParallel: false,
  workers: 1,
  timeout: 30000,
  use: { baseURL: 'http://127.0.0.1:5176', browserName: 'chromium', trace: 'retain-on-failure' },
  webServer: { command: 'npm run dev -- --port 5176', url: 'http://127.0.0.1:5176', reuseExistingServer: !process.env.CI },
});
