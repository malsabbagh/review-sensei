import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const ROOT = new URL("..", import.meta.url);
const FORBIDDEN_SOURCE = ["ctx.storage", "sql.exec", "DurableObject", "setAlarm"];
const FORBIDDEN_BINDINGS = ["durable_objects", "kv_namespaces", "d1_databases", "r2_buckets"];

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

describe("worker storage ban", () => {
  it("rejects durable storage APIs under deploy/cloudflare/src", () => {
    const source = filesUnder(join(ROOT.pathname, "src"));
    expect(source.length).toBeGreaterThan(0);
    for (const path of source) {
      const text = readFileSync(path, "utf8");
      for (const token of FORBIDDEN_SOURCE) {
        expect(text, path).not.toContain(token);
      }
    }
  });

  it("rejects durable bindings in wrangler.jsonc", () => {
    const text = readFileSync(join(ROOT.pathname, "wrangler.jsonc"), "utf8");
    for (const token of FORBIDDEN_BINDINGS) {
      expect(text).not.toContain(token);
    }
    expect(text).toContain('"deleted_classes"');
    expect(text).toContain('"DeliveryLedger"');
    expect(text).toContain('"BrokerLedger"');
    expect(text).not.toContain("ratelimits");
  });
});
