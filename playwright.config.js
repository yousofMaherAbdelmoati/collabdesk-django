const { defineConfig } = require("@playwright/test");

const baseURL = process.env.E2E_BASE_URL || "http://127.0.0.1:8000";

module.exports = defineConfig({
  testDir: "./tests/e2e",
  timeout: 90_000,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: [["list"], ["html", { open: "never" }]],
  use: {
    baseURL,
    browserName: "chromium",
    // Local development can use an already-installed Chrome. CI instead uses
    // the browser provisioned by `npx playwright install chromium`.
    launchOptions: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE
      ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE, args: ["--no-sandbox"] }
      : undefined,
    screenshot: "only-on-failure",
    trace: "retain-on-failure"
  }
});
