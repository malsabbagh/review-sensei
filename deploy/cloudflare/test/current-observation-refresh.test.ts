import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { consumingBroker, SHA } from "./epic238-consuming-broker-fixture";
import { resetOidcJwksCacheForTesting } from "../src/oidc";
import { BrokerDispatchControl } from "../src/broker-dispatch-control";

beforeEach(() => resetOidcJwksCacheForTesting());
afterEach(() => vi.unstubAllGlobals());
const effect = "e".repeat(64);
const control = (max_dispatches: number) => new BrokerDispatchControl({ schema_version: "1.0", operation_id: "a".repeat(64), request_id: "b".repeat(64), deadline_unix_ms: Date.now() + 60_000, max_dispatches });
async function prepared(purpose: "review" | "command" | "reply", role = true) {
  const fixture = await consumingBroker(":memory:", undefined, true);
  const state = { installation: 2468, selection: "selected", access: true, permission: "admin", role: "admin", actorId: 12345678, actorLogin: "octocat", ownerId: 77, tokenPermission: "write" };
  const requests: { method: string; path: string }[] = [];
  vi.stubGlobal("fetch", async (input: string | URL | Request, init?: RequestInit) => {
    const url = new URL(input instanceof Request ? input.url : String(input));
    requests.push({ method: init?.method ?? "GET", path: url.pathname });
    // Exercise actual public Git failure + annotated REST ref/tag: all three
    // worst-case physical runtime reads, not a mocked GitHubApi method.
    if (url.hostname === "github.com") return new Response("unavailable", { status: 503 });
    if (url.pathname.endsWith("/git/ref/tags/v5")) return Response.json({ object: { type: "tag", sha: "f".repeat(40) } });
    if (url.pathname.endsWith(`/git/tags/${"f".repeat(40)}`)) return Response.json({ object: { type: "commit", sha: SHA } });
    if (url.pathname === "/repos/acme/widgets/installation") return state.access ? Response.json({ id: state.installation, repository_selection: state.selection, permissions: { metadata: "read", pull_requests: "write" } }) : new Response(null, { status: 404 });
    if (url.pathname === "/repos/acme/widgets") return Response.json({ id: 987654321, fork: false, full_name: "acme/widgets", owner: { id: state.ownerId, login: "acme" } });
    if (url.pathname.includes("/collaborators/")) return Response.json({ permission: state.permission, role_name: state.role, user: { id: state.actorId, login: state.actorLogin, type: "User" } });
    if (url.pathname.endsWith("/access_tokens") && state.tokenPermission !== "write") return Response.json({ token: "synthetic-scoped", permissions: { metadata: "read", pull_requests: state.tokenPermission } });
    return fixture.transport(input, init);
  });
  const issued = await fixture.broker.exchange({ oidc_token: await fixture.oidcToken(), capability: "review_session", session: { repository_id: 987654321, pull_request: 7, head_sha: SHA }, session_attestation: { ...fixture.authority(purpose), version: 4 }, ...(role ? { observe_actor_role: true } : {}) });
  requests.length = 0;
  return { fixture, issued, state, requests };
}

describe("source-produced refresh, not effect execution or continuous trust", () => {
  it.each(["review", "command", "reply"] as const)("refreshes %s under exact full 8/9 request ceiling with immutable original", async purpose => {
    const { fixture, issued, requests } = await prepared(purpose);
    const original = JSON.stringify(issued.session_attestation);
    const count = purpose === "review" ? 8 : 9;
    const budget = control(count);
    const result = await fixture.broker.refreshSessionObservation(issued.session_grant, issued.session_attestation, effect, budget);
    expect(requests).toHaveLength(count);
    expect(budget.receipt().dispatched).toBe(count);
    expect(result.session_attestation).toEqual(issued.session_attestation);
    expect(JSON.stringify(issued.session_attestation)).toBe(original);
    expect(result.current_observation.actor).toMatchObject({ repository_permission: "admin", repository_role: "admin", id: 12345678 });
    expect(result.observation_binding.effect_sha256).toBe(effect);
    expect(result.observation_binding.original_attestation_sha256).toMatch(/^[a-f0-9]{64}$/);
    expect(result).not.toHaveProperty("token");
    expect(requests.filter(r => r.method !== "GET").map(r => r.path)).toEqual(["/app/installations/2468/access_tokens"]);
  });
  it.each(["review", "command", "reply"] as const)("refuses one-short %s before any physical read/token issuance", async purpose => {
    const { fixture, issued, requests } = await prepared(purpose);
    const budget = control(purpose === "review" ? 7 : 8);
    await expect(fixture.broker.refreshSessionObservation(issued.session_grant, issued.session_attestation, effect, budget)).rejects.toThrow("broker_control_budget_exhausted");
    expect(requests).toEqual([]); expect(budget.receipt().dispatched).toBe(0);
  });
  it.each(["role downgrade", "identity", "login", "reinstall", "selection", "access removed", "owner", "head", "source", "token permission"])("holds %s without native writes or changing original facts", async change => {
    const { fixture, issued, state, requests } = await prepared("command");
    const original = JSON.stringify(issued.session_attestation);
    if (change === "role downgrade") { state.permission = "read"; state.role = "read"; }
    if (change === "identity") state.actorId++;
    if (change === "login") state.actorLogin = "other";
    if (change === "reinstall") state.installation++;
    if (change === "selection") state.selection = "all";
    if (change === "access removed") state.access = false;
    if (change === "owner") state.ownerId++;
    if (change === "head") fixture.githubInputs.head = "b".repeat(40);
    if (change === "source") fixture.githubInputs.source.body += " changed";
    if (change === "token permission") state.tokenPermission = "read";
    await expect(fixture.broker.refreshSessionObservation(issued.session_grant, issued.session_attestation, effect, control(9))).rejects.toThrow();
    expect(JSON.stringify(issued.session_attestation)).toBe(original);
    expect(requests.filter(r => r.method !== "GET" && !r.path.endsWith("/access_tokens"))).toEqual([]);
    expect(requests.length).toBeLessThanOrEqual(9);
  });
  it("old v4 observation still verifies, but cannot supply an original actor role", async () => {
    const { fixture, issued, requests } = await prepared("review", false);
    expect(await fixture.broker.verifySessionGrant(issued.session_grant, issued.session_attestation)).toEqual(issued.session_attestation);
    requests.length = 0;
    await expect(fixture.broker.refreshSessionObservation(issued.session_grant, issued.session_attestation, effect, control(8))).rejects.toThrow("broker_original_actor_role_unavailable");
    expect(requests).toEqual([]);
  });
  it("refuses malformed binding and fabricated original role before fresh observations", async () => {
    const { fixture, issued, requests } = await prepared("review");
    await expect(fixture.broker.refreshSessionObservation(issued.session_grant, issued.session_attestation, "bad", control(8))).rejects.toThrow("broker_effect_binding_invalid");
    expect(requests).toEqual([]);
    const forged = structuredClone(issued.session_attestation)!;
    forged.current_observation!.actor.repository_permission = "read";
    await expect(fixture.broker.refreshSessionObservation(issued.session_grant, forged, effect, control(8))).rejects.toThrow("broker_session_grant_invalid");
    expect(requests).toHaveLength(3); // Runtime only; no role read or token mint.
  });
});
