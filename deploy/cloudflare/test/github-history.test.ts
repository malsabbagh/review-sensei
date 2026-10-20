import { afterEach, describe, expect, it, vi } from "vitest";
import type { WorkerEnv } from "../src/env";
import { GitHubApi, readBoundedHistory } from "../src/github-api";
import shape from "./fixtures/pr316-history-shape.json";

const SHA = "a".repeat(40);
const MARKER = "<!-- review-sensei-operation -->";
const bytes = (value: unknown) => new TextEncoder().encode(JSON.stringify(value)).byteLength;

function history() {
  const result = shape.record_sizes.map((size, index) => {
    const suffix = index === shape.record_sizes.length - 1 ? MARKER : "";
    const item = {
      id: index + 1,
      user: { login: "reviewsensei[bot]", type: "Bot" },
      body: "x".repeat(shape.body_sizes[index]! - suffix.length) + suffix,
      opaque: { retained: true, padding: "" },
    };
    const padding = size - bytes(item);
    expect(padding).toBeGreaterThanOrEqual(0);
    item.opaque.padding = "m".repeat(padding);
    expect(bytes(item)).toBe(size);
    return item;
  });
  expect(bytes(result)).toBe(shape.captured_response_bytes);
  return result;
}

function client(key = "unused-by-fixture") {
  const env: WorkerEnv = {
    GITHUB_APP_ID: "12345",
    GITHUB_APP_PRIVATE_KEY: key,
    GITHUB_APP_WEBHOOK_SECRET: "unused-by-fixture",
    PUBLIC_WORKFLOW_TAG: "v5",
    GITHUB_API_URL: "https://api.example.test",
  };
  const api = new GitHubApi(env);
  vi.spyOn(api, "appBotLogin").mockResolvedValue("reviewsensei[bot]");
  return api;
}

afterEach(() => vi.restoreAllMocks());

function pageServer(items: unknown[], streamed = false) {
  const calls: Array<{ size: number; page: number }> = [];
  let canceled = 0;
  const mock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = new URL(typeof input === "string" ? input : input instanceof URL ? input.href : input.url);
    expect(init?.method).toBe("GET");
    const size = Number(url.searchParams.get("per_page"));
    const page = Number(url.searchParams.get("page"));
    calls.push({ size, page });
    const selected = items.slice((page - 1) * size, page * size);
    const encoded = new TextEncoder().encode(JSON.stringify(selected));
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encoded);
        if (encoded.byteLength <= 512 * 1024) controller.close();
      },
      cancel() { canceled += 1; },
    });
    return new Response(body, { headers: streamed ? {} : { "content-length": String(encoded.byteLength) } });
  });
  return { calls, mock, canceled: () => canceled };
}

function scan(api: GitHubApi) {
  return readBoundedHistory(
    (size, page) => api.request("GET", `/history?per_page=${size}&page=${page}`, "fixture-token"),
    "fixture_history_failed",
  );
}

async function ephemeralKey() {
  const pair = await crypto.subtle.generateKey({
    name: "RSASSA-PKCS1-v1_5", modulusLength: 2048,
    publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-256",
  }, true, ["sign", "verify"]);
  const der = new Uint8Array(await crypto.subtle.exportKey("pkcs8", pair.privateKey));
  return `-----BEGIN PRIVATE KEY-----\n${btoa(String.fromCharCode(...der))}\n-----END PRIVATE KEY-----`;
}

describe("bounded broker session history", () => {
  it("finds the final trusted operation in the captured 27-comment response shape", async () => {
    const items = history();
    const requested: Array<{ size: number; page: number; ids: number[] }> = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = new URL(typeof input === "string" ? input : input instanceof URL ? input.href : input.url);
      expect(init?.method).toBe("GET");
      if (url.pathname.endsWith("/reviews")) return new Response("[]");
      expect(url.pathname).toBe("/repos/acme/widgets/issues/7/comments");
      const size = Number(url.searchParams.get("per_page"));
      const page = Number(url.searchParams.get("page"));
      const selected = items.slice((page - 1) * size, page * size);
      requested.push({ size, page, ids: selected.map(item => item.id) });
      const body = JSON.stringify(selected);
      return new Response(body, { headers: { "content-length": String(new TextEncoder().encode(body).byteLength) } });
    });
    await expect(client().sessionAlreadyKnown("acme/widgets", 7, SHA, "fixture-token"))
      .resolves.toBe(true);
    expect(requested.length).toBeLessThanOrEqual(10);
    expect(requested.filter(item => item.size === 5).flatMap(item => item.ids))
      .toEqual(items.map(item => item.id));
  });

  it.each([false, true])("preserves every full object and cancels oversized bodies (streamed=%s)", async streamed => {
    const items = history();
    const server = pageServer(items, streamed);
    await expect(scan(client())).resolves.toEqual(items);
    expect(server.calls).toEqual([
      { size: 100, page: 1 }, { size: 50, page: 1 }, { size: 25, page: 1 },
      ...Array.from({ length: 6 }, (_, index) => ({ size: 5, page: index + 1 })),
    ]);
    expect(server.canceled()).toBe(3);
  });

  it.each([100, 250])("retrieves %s metadata-heavy objects within the original ten requests", async count => {
    const items = Array.from({ length: count }, (_, id) => ({ id, body: "x".repeat(8000), opaque: { keep: id } }));
    const server = pageServer(items);
    await expect(scan(client())).resolves.toEqual(items);
    expect(server.calls.length).toBeLessThanOrEqual(10);
  });

  it("discards a partial walk when a later page needs a smaller size", async () => {
    const items = Array.from({ length: 200 }, (_, id) => ({ id, body: "x".repeat(id < 100 ? 50 : 6000) }));
    const server = pageServer(items);
    await expect(scan(client())).resolves.toEqual(items);
    expect(server.calls.slice(0, 3)).toEqual([
      { size: 100, page: 1 }, { size: 100, page: 2 }, { size: 50, page: 1 },
    ]);
    expect(server.calls.length).toBe(7);
  });

  it("refuses one oversized object even at a one-item page", async () => {
    const server = pageServer([{ id: 1, body: "x".repeat(512 * 1024) }]);
    await expect(scan(client())).rejects.toThrow("github_response_too_large");
    expect(server.calls.map(call => call.size)).toEqual([100, 50, 25, 5, 1]);
    expect(server.canceled()).toBe(5);
  });

  it("charges failed attempts against the original ten-request allowance", async () => {
    const items = Array.from({ length: 250 }, (_, id) => ({ id, body: "x".repeat(20_000) }));
    const server = pageServer(items);
    await expect(scan(client())).rejects.toThrow("fixture_history_failed");
    expect(server.calls).toHaveLength(10);
  });

  it("refuses an unterminated 1000-item inventory without an eleventh request", async () => {
    const server = pageServer(Array.from({ length: 1000 }, (_, id) => ({ id })));
    await expect(scan(client())).rejects.toThrow("fixture_history_failed");
    expect(server.calls).toHaveLength(10);
  });

  it.each([
    { status: 403, data: [] },
    { status: 200, data: {} },
    { status: 200, data: Array.from({ length: 101 }, (_, id) => ({ id })) },
  ])("does not retry an invalid/authentication response: %j", async response => {
    const read = vi.fn(async () => response);
    await expect(readBoundedHistory(read, "fixture_history_failed")).rejects.toThrow("fixture_history_failed");
    expect(read).toHaveBeenCalledTimes(1);
  });

  it("does not turn a failed read after earlier success into partial authority", async () => {
    const read = vi.fn(async (_size: number, page: number) => {
      if (page === 2) throw new Error("github_session_witness_failed");
      return { status: 200, data: Array.from({ length: 100 }, (_, id) => ({ id })) };
    });
    await expect(readBoundedHistory(read, "fixture_history_failed")).rejects.toThrow("github_session_witness_failed");
    expect(read).toHaveBeenCalledTimes(2);
  });

  it("does not trust a foreign bot's marker after shrinking pages", async () => {
    const items = history();
    items[items.length - 1]!.user.login = "another-app[bot]";
    const fetch = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      expect(init?.method).toBe("GET");
      const url = new URL(typeof input === "string" ? input : input instanceof URL ? input.href : input.url);
      if (url.pathname.endsWith("/reviews")) return new Response("[]");
      const size = Number(url.searchParams.get("per_page"));
      const page = Number(url.searchParams.get("page"));
      const body = JSON.stringify(items.slice((page - 1) * size, page * size));
      return new Response(body, { headers: { "content-length": String(new TextEncoder().encode(body).byteLength) } });
    });
    await expect(client().sessionAlreadyKnown("acme/widgets", 7, SHA, "fixture-token")).resolves.toBe(false);
    expect(fetch.mock.calls.every(([, init]) => init?.method === "GET")).toBe(true);
  });

  it("keeps exact-head review authority separate from operation-marker discovery", async () => {
    const reviews = history().map(item => ({ ...item, commit_id: "b".repeat(40), state: "COMMENTED" }));
    const fetch = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      expect(init?.method).toBe("GET");
      const url = new URL(typeof input === "string" ? input : input instanceof URL ? input.href : input.url);
      const size = Number(url.searchParams.get("per_page"));
      const page = Number(url.searchParams.get("page"));
      const body = JSON.stringify(url.pathname.endsWith("/reviews") ? reviews.slice((page - 1) * size, page * size) : []);
      return new Response(body, { headers: { "content-length": String(new TextEncoder().encode(body).byteLength) } });
    });
    await expect(client().sessionAlreadyKnown("acme/widgets", 7, SHA, "fixture-token")).resolves.toBe(false);
    expect(fetch.mock.calls.length).toBeLessThanOrEqual(20);
  });

  it("recognizes a current-head trusted review after shrinking without reading comments", async () => {
    const reviews = history().map(item => ({ ...item, commit_id: SHA, state: "COMMENTED" }));
    const server = pageServer(reviews);
    await expect(client().sessionAlreadyKnown("acme/widgets", 7, SHA, "fixture-token"))
      .resolves.toBe(true);
    expect(server.calls.length).toBeLessThanOrEqual(10);
    expect(server.mock.mock.calls.every(([input]) => String(input).includes("/reviews?"))).toBe(true);
  });

  it.each([false, true])("uses the same bounded reader for installation inventory (invalid=%s)", async invalid => {
    const repos = history().map((item, index) => ({
      ...item, full_name: invalid && index === 26 ? "acme/repo/invalid" : `acme/repo-${index}`,
    }));
    const reads: Array<{ size: number; page: number }> = [];
    const fetch = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = new URL(typeof input === "string" ? input : input instanceof URL ? input.href : input.url);
      if (url.pathname === "/app/installations/2468/access_tokens") {
        expect(init?.method).toBe("POST");
        expect(init?.body).toBeUndefined();
        return new Response(JSON.stringify({
          token: "fixture-installation-token", expires_at: new Date(Date.now() + 60_000).toISOString(),
        }), { status: 201 });
      }
      expect(init?.method).toBe("GET");
      expect(url.pathname).toBe("/installation/repositories");
      const size = Number(url.searchParams.get("per_page"));
      const page = Number(url.searchParams.get("page"));
      reads.push({ size, page });
      const body = JSON.stringify({ repositories: repos.slice((page - 1) * size, page * size) });
      return new Response(body, { headers: { "content-length": String(new TextEncoder().encode(body).byteLength) } });
    });
    const result = client(await ephemeralKey()).installationRepositories(2468);
    if (invalid) await expect(result).rejects.toThrow("github_installation_repository_invalid");
    else await expect(result).resolves.toEqual(repos.map(repo => repo.full_name));
    expect(reads).toHaveLength(9);
    expect(fetch.mock.calls.filter(([, init]) => init?.method !== "GET")).toHaveLength(1);
  });
});
