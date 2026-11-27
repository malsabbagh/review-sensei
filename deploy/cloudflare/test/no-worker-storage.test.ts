import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import contract from "../../../src/review_sensei/schemas/v6-coordinator-contract.json";

const ROOT = new URL("..", import.meta.url);
const FORBIDDEN_SOURCE = ["ctx.storage", "sql.exec", "DurableObject", "setAlarm"];
const FORBIDDEN_BINDINGS = ["kv_namespaces", "d1_databases", "r2_buckets", "ratelimits", "containers"];

function filesUnder(directory: string): string[] {
  const found: string[] = [];
  for (const entry of readdirSync(directory)) {
    const path = join(directory, entry);
    if (statSync(path).isDirectory()) {
      found.push(...filesUnder(path));
    } else {
      found.push(path);
    }
  }
  return found;
}

describe("worker storage and v6 coordinator boundary", () => {
  it("restricts durable storage APIs solely to V6PrCoordinator", () => {
    const source = filesUnder(join(ROOT.pathname, "src"));
    expect(source.length).toBeGreaterThan(0);
    for (const path of source) {
      const text = readFileSync(path, "utf8");
      if (path.endsWith("/v6-pr-coordinator.ts")) {
        expect(text, path).not.toContain("setAlarm");
        expect(text, path).not.toContain("DeliveryLedger");
        expect(text, path).not.toContain("BrokerLedger");
        continue;
      }
      if (path.endsWith("/worker.ts")) {
        for (const token of ["ctx.storage", "sql.exec", "setAlarm", "DeliveryLedger", "BrokerLedger"]) {
          expect(text, path).not.toContain(token);
        }
        continue;
      }
      if (path.endsWith("/env.ts")) {
        for (const token of ["ctx.storage", "sql.exec", "setAlarm", "DeliveryLedger", "BrokerLedger"]) {
          expect(text, path).not.toContain(token);
        }
        continue;
      }
      for (const token of FORBIDDEN_SOURCE) {
        expect(text, path).not.toContain(token);
      }
    }
  });

  it("permits only V6PrCoordinator in wrangler.jsonc and keeps v5 pinned", () => {
    const text = readFileSync(join(ROOT.pathname, "wrangler.jsonc"), "utf8");
    const parsed = JSON.parse(text) as {
      vars: Record<string, unknown>;
      durable_objects?: { bindings?: Array<{ name: string; class_name: string }> };
      migrations?: Array<{ tag: string; new_sqlite_classes?: string[]; deleted_classes?: string[] }>;
    };
    for (const token of FORBIDDEN_BINDINGS) {
      expect(text).not.toContain(token);
    }
    expect(parsed.vars.PUBLIC_WORKFLOW_TAG).toBe("v5");
    expect(parsed.durable_objects?.bindings).toEqual([
      { name: "V6_PR_COORDINATOR", class_name: "V6PrCoordinator" },
    ]);
    const v3 = parsed.migrations?.find((m) => m.tag === "v3");
    expect(v3?.deleted_classes).toEqual(["DeliveryLedger", "BrokerLedger"]);
    const v4 = parsed.migrations?.find((m) => m.tag === "v4");
    expect(v4?.new_sqlite_classes).toEqual(["V6PrCoordinator"]);
    expect(parsed.migrations?.length).toBe(4);
  });

  it("forbids storing any contract never_keep_fields in v6-pr-coordinator.ts", () => {
    const coordText = readFileSync(
      join(ROOT.pathname, "src", "v6-pr-coordinator.ts"),
      "utf8",
    );
    const tableMatch = /CREATE TABLE IF NOT EXISTS v6_pr_slot \(([\s\S]*?)\)/.exec(coordText);
    expect(tableMatch).not.toBeNull();
    const columns = tableMatch![1]
      .split("\n")
      .map((line) => line.trim().split(/\s+/)[0])
      .filter((col) => Boolean(col) && col !== ")");

    expect(new Set(columns)).toEqual(new Set(contract.coordinator_storage.keep_fields));
    for (const banned of contract.coordinator_storage.never_keep_fields) {
      expect(columns).not.toContain(banned);
    }
  });

  it("ensures v6-pr-coordinator.ts performs zero GitHub PR data-plane write mutations", () => {
    const coordText = readFileSync(
      join(ROOT.pathname, "src", "v6-pr-coordinator.ts"),
      "utf8",
    );
    const githubApiText = readFileSync(
      join(ROOT.pathname, "src", "github-api.ts"),
      "utf8",
    );
    const forbiddenWriteEndpoints = [
      "/reviews",
      "/check-runs",
      "/git/refs",
      "/graphql",
      "/comments/${",
      "resolveReviewThread",
      "unresolveReviewThread",
      "addPullRequestReviewThreadReply",
    ];
    for (const marker of forbiddenWriteEndpoints) {
      if (marker !== "/reviews" && marker !== "/comments/${") {
        expect(coordText).not.toContain(marker);
      }
    }
    expect(coordText).not.toMatch(/pulls\/\$\{[^}]+\}\/reviews/);
    expect(coordText).not.toMatch(/issues\/\$\{[^}]+\}\/comments/);
    expect(githubApiText).not.toContain("resolveReviewThread");
    expect(githubApiText).not.toContain("/check-runs");
    expect(githubApiText).not.toMatch(/"POST"[^;]*\/pulls\//);
    expect(githubApiText).not.toMatch(/"PATCH"[^;]*\/issues\/comments/);
  });
});
