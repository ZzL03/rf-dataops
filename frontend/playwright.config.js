import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests",
  timeout: 60000,
  workers: 1,
  reporter: "list",
  outputDir: "../artifacts/playwright-results",
  use: {
    baseURL: process.env.RF_E2E_URL || "http://127.0.0.1:18080",
    channel: "msedge",
    headless: true,
    viewport: { width: 1440, height: 1000 },
    trace: "off",
  },
});
