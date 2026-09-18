import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { mkdtempSync, readdirSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { RELEASED_RUNNER_SWITCH_V4_SHA256 } from "../src/released-runner-switch-v4-caller";

describe("worker bundle", () => {
  it("ships the released runner-switch caller fixture with the deploy artifact", () => {
    const outdir = mkdtempSync(join(tmpdir(), "reviewsensei-worker-bundle-"));
    execFileSync(
      "npx",
      ["wrangler", "deploy", "--dry-run", "--outdir", outdir],
      {
        cwd: fileURLToPath(new URL("..", import.meta.url)),
        stdio: "pipe",
      },
    );
    const bundle = readFileSync(join(outdir, "worker.js"), "utf8");
    const fixtureName = readdirSync(outdir).find((name) =>
      name.endsWith("released-v4-resolve-trigger-runner-switch.yml"),
    );
    expect(fixtureName).toBeTruthy();
    const bundledFixture = readFileSync(join(outdir, fixtureName!), "utf8");
    const canonical = readFileSync(
      new URL(
        "../../../tests/fixtures/setup-legacy/released-v4-resolve-trigger-runner-switch.yml",
        import.meta.url,
      ),
      "utf8",
    );
    expect(bundle).toContain("released-v4-resolve-trigger-runner-switch.yml");
    expect(bundle).not.toContain("readFileSync");
    expect(bundledFixture).toBe(canonical);
    expect(createHash("sha256").update(bundledFixture).digest("hex")).toBe(
      RELEASED_RUNNER_SWITCH_V4_SHA256,
    );
  });
});
