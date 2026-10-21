import { BrokerDispatchControl } from "../src/broker-dispatch-control";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { resetOidcJwksCacheForTesting, verifyOidcAssertion } from "../src/oidc";

const ISSUER = "https://token.actions.githubusercontent.com";
const AUDIENCE = "sts.reviewsensei.dev";
const NOW = 1_700_000_000;
let privateKey: CryptoKey;
let jwk: JsonWebKey;

function base64Url(bytes: Uint8Array): string {
  return Buffer.from(bytes).toString("base64url");
}

function encodedJson(value: unknown): string {
  return base64Url(new TextEncoder().encode(JSON.stringify(value)));
}

function claims(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  const sha = "a".repeat(40);
  return {
    iss: ISSUER,
    aud: AUDIENCE,
    exp: NOW + 300,
    iat: NOW - 30,
    sub: "repo:acme/widgets:pull_request",
    jti: "4b5e6f70-1234-4567-890a-123456789abc",
    repository: "acme/widgets",
    repository_owner: "acme",
    repository_id: "987654321",
    actor: "octocat",
    actor_id: "12345678",
    event_name: "pull_request",
    workflow: "ReviewSensei review",
    workflow_ref: "acme/widgets/.github/workflows/review-sensei-review.yml@refs/heads/main",
    workflow_sha: sha,
    job_workflow_ref: `malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@${sha}`,
    job_workflow_sha: sha,
    ref: "refs/pull/7/merge",
    sha,
    run_id: "10000000001",
    run_number: "42",
    run_attempt: "1",
    runner_environment: "github-hosted",
    ...overrides,
  };
}

async function token(payload: Record<string, unknown>, signingKey = privateKey): Promise<string> {
  const header = encodedJson({ alg: "RS256", typ: "JWT", kid: "test-key" });
  const body = encodedJson(payload);
  const input = `${header}.${body}`;
  const signature = await crypto.subtle.sign(
    "RSASSA-PKCS1-v1_5",
    signingKey,
    new TextEncoder().encode(input),
  );
  return `${input}.${base64Url(new Uint8Array(signature))}`;
}

beforeAll(async () => {
  const pair = await crypto.subtle.generateKey(
    {
      name: "RSASSA-PKCS1-v1_5",
      modulusLength: 2048,
      publicExponent: new Uint8Array([1, 0, 1]),
      hash: "SHA-256",
    },
    true,
    ["sign", "verify"],
  );
  privateKey = pair.privateKey;
  jwk = {
    ...(await crypto.subtle.exportKey("jwk", pair.publicKey)),
    kid: "test-key",
    alg: "RS256",
    use: "sig",
  };
});

afterEach(() => vi.unstubAllGlobals());
beforeEach(() => resetOidcJwksCacheForTesting());

function serveJwks(keys: JsonWebKey[] = [jwk]): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn(async () =>
    new Response(JSON.stringify({ keys }), {
      status: 200,
      headers: { "content-type": "application/json" },
    }),
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("GitHub Actions OIDC validation", () => {
  it("verifies RS256 and parses GitHub decimal-string identity claims", async () => {
    const fetchMock = serveJwks();
    const result = await verifyOidcAssertion(await token(claims()), { now: NOW });

    expect(result.repository_id).toBe(987654321);
    expect(result.actor_id).toBe(12345678);
    expect(result.repository).toBe("acme/widgets");
    expect(fetchMock).toHaveBeenCalledWith(`${ISSUER}/.well-known/jwks`, {
      headers: { accept: "application/json" }, redirect: "manual", signal: expect.any(AbortSignal),
    });
  });

  it.each([
    ["issuer", { iss: "https://attacker.invalid" }, "oidc_identity_invalid"],
    ["audience", { aud: "another-service" }, "oidc_audience_invalid"],
    ["expiration", { exp: NOW - 31 }, "oidc_time_invalid"],
    ["issued-at", { iat: NOW + 31 }, "oidc_time_invalid"],
    ["issued-at age", { iat: NOW - 301 }, "oidc_time_invalid"],
  ])("rejects an invalid %s", async (_name, overrides, message) => {
    serveJwks();
    await expect(verifyOidcAssertion(await token(claims(overrides)), { now: NOW })).rejects.toThrow(message);
  });

  it("accepts an assertion issued exactly five minutes ago", async () => {
    serveJwks();
    await expect(verifyOidcAssertion(await token(claims({ iat: NOW - 300 })), { now: NOW })).resolves.toMatchObject({
      repository: "acme/widgets",
    });
  });

  it("rejects a token whose signature does not match the advertised key", async () => {
    const otherPair = await crypto.subtle.generateKey(
      {
        name: "RSASSA-PKCS1-v1_5",
        modulusLength: 2048,
        publicExponent: new Uint8Array([1, 0, 1]),
        hash: "SHA-256",
      },
      true,
      ["sign", "verify"],
    );
    serveJwks();
    await expect(
      verifyOidcAssertion(await token(claims(), otherPair.privateKey), { now: NOW }),
    ).rejects.toThrow("oidc_signature_invalid");
  });

  it("caches public JWKS across repeated forged-signature attempts", async () => {
    const otherPair = await crypto.subtle.generateKey(
      {
        name: "RSASSA-PKCS1-v1_5",
        modulusLength: 2048,
        publicExponent: new Uint8Array([1, 0, 1]),
        hash: "SHA-256",
      },
      true,
      ["sign", "verify"],
    );
    const fetchMock = serveJwks();
    const forged = await token(claims(), otherPair.privateKey);

    await expect(verifyOidcAssertion(forged, { now: NOW })).rejects.toThrow(
      "oidc_signature_invalid",
    );
    await expect(verifyOidcAssertion(forged, { now: NOW })).rejects.toThrow(
      "oidc_signature_invalid",
    );
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["numeric repository id", { repository_id: 987654321 }],
    ["non-decimal repository id", { repository_id: "98x" }],
    ["numeric actor id", { actor_id: 12345678 }],
    ["unsafe actor id", { actor_id: "9007199254740992" }],
  ])("rejects %s", async (_name, overrides) => {
    serveJwks();
    await expect(verifyOidcAssertion(await token(claims(overrides)), { now: NOW })).rejects.toThrow(
      "oidc_claims_invalid",
    );
  });
  it("isolates simultaneous JWKS misses, cancels only the expired caller and fences late cache writes", async () => {
    const assertion = await token(claims());
    vi.useFakeTimers();
    const pending: {signal: AbortSignal, resolve: (value: Response) => void}[] = [];
    const fetchMock = vi.fn((_url, init) => new Promise<Response>(resolve => pending.push({signal: init.signal, resolve})));
    vi.stubGlobal("fetch", fetchMock);
    const control = new BrokerDispatchControl({schema_version: "1.0", operation_id: "a".repeat(64), request_id: "b".repeat(64), deadline_unix_ms: Date.now()+30, max_dispatches: 1});
    const first = verifyOidcAssertion(assertion, {now: NOW, dispatchControl: control}).then(() => "accepted", error => error.message);
    const second = verifyOidcAssertion(assertion, {now: NOW});
    try {
      await vi.advanceTimersByTimeAsync(30);
      await expect(first).resolves.toBe("broker_control_deadline_exhausted");
      expect(pending).toHaveLength(2);
      expect(pending[0].signal.aborted).toBe(true);
      expect(pending[1].signal.aborted).toBe(false);
      pending[1].resolve(new Response(JSON.stringify({keys: [jwk]})));
      await expect(second).resolves.toMatchObject({repository: "acme/widgets"});
      const lateCancel = vi.fn();
      pending[0].resolve(new Response(new ReadableStream({cancel: lateCancel})));
      await vi.advanceTimersByTimeAsync(0);
      expect(lateCancel).toHaveBeenCalledOnce();
      await expect(verifyOidcAssertion(assertion, {now: NOW})).resolves.toMatchObject({repository: "acme/widgets"});
      expect(fetchMock).toHaveBeenCalledTimes(2);
    } finally {vi.useRealTimers();}
  });

  it("aborts stalled JWKS headers and permits a fresh successful cache miss", async () => {
    const assertion = await token(claims());
    vi.useFakeTimers();
    let signal!: AbortSignal;
    const fetchMock = vi.fn((_url, init) => {signal=init.signal; return new Promise<Response>(() => {});});
    vi.stubGlobal("fetch", fetchMock);
    const first = verifyOidcAssertion(assertion, {now: NOW}).then(() => "accepted", error => error.message);
    await vi.advanceTimersByTimeAsync(10_000);
    await expect(first).resolves.toBe("broker_upstream_timeout");
    expect(signal.aborted).toBe(true);
    fetchMock.mockImplementation(async () => new Response(JSON.stringify({keys: [jwk]})));
    await expect(verifyOidcAssertion(assertion, {now: NOW})).resolves.toMatchObject({repository: "acme/widgets"});
    expect(fetchMock).toHaveBeenCalledTimes(2);
    vi.useRealTimers();
  });

  it("cancels stalled JWKS bodies, keeps completed TTL and refreshes after expiry", async () => {
    const assertion = await token(claims());
    vi.useFakeTimers();
    const cancel=vi.fn();
    const fetchMock=vi.fn(async () => new Response(new ReadableStream({cancel})));
    vi.stubGlobal("fetch", fetchMock);
    const first=verifyOidcAssertion(assertion,{now:NOW}).then(() => "accepted",error => error.message);
    await vi.advanceTimersByTimeAsync(10_000);
    await expect(first).resolves.toBe("broker_upstream_timeout");
    expect(cancel).toHaveBeenCalledOnce();
    fetchMock.mockImplementation(async () => new Response(JSON.stringify({keys:[jwk]})));
    await verifyOidcAssertion(assertion,{now:NOW});
    await verifyOidcAssertion(assertion,{now:NOW});
    expect(fetchMock).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(300_001);
    await verifyOidcAssertion(assertion,{now:NOW});
    expect(fetchMock).toHaveBeenCalledTimes(3);
    vi.useRealTimers();
  });

});
