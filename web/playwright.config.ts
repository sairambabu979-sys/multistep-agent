import { defineConfig, devices } from "@playwright/test";

// The real workbench against the real API: `msa serve` on seeded runs, and `next start`.
const STATE = ".e2e-state";
const API = "http://127.0.0.1:8799";

export default defineConfig({
  testDir: "tests/e2e",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"]],
  use: { baseURL: "http://127.0.0.1:3100", trace: "off" },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: [
    {
      command: `cd .. && uv run python web/tests/e2e/seed.py web/${STATE} && uv run msa --config web/${STATE}/msa.toml serve --repo web/${STATE}/repo --db web/${STATE}/runs.db --port 8799`,
      url: `${API}/api/health`,
      timeout: 180_000,
      reuseExistingServer: false,
    },
    {
      command: `NEXT_PUBLIC_AEGIS_API=${API} npx next build && npx next start -H 127.0.0.1 -p 3100`,
      url: "http://127.0.0.1:3100",
      timeout: 300_000,
      reuseExistingServer: false,
    },
  ],
});
