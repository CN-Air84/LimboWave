import { defineConfig } from "@playwright/test";
export default defineConfig({
  testDir: "./tests",
  timeout: 45000,
  expect: { timeout: 10000 },
  fullyParallel: false,
  workers: 1,
  reporter: "list",
  use: {
    baseURL: "http://127.0.0.1:8877",
    browserName: "chromium",
    viewport: { width: 320, height: 740 },
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
});
