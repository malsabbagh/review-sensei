import { afterEach, describe, expect, it, vi } from "vitest";
import { consumingBroker } from "./epic238-consuming-broker-fixture";
import { resetOidcJwksCacheForTesting } from "../src/oidc";

const control = (maximum: number) => ({
  schema_version: "1.0", operation_id: "c".repeat(64), request_id: "d".repeat(64),
  deadline_unix_ms: Date.now() + 60_000, max_dispatches: maximum,
});
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); resetOidcJwksCacheForTesting(); });

describe("media check capability physical funding", () => {
  it("cannot use the warm-only five-dispatch allowance on a cold worker", async () => {
    const fixture = await consumingBroker();
    vi.stubGlobal("fetch", fixture.transport);
    resetOidcJwksCacheForTesting();
    await expect(fixture.broker.exchange({oidc_token: await fixture.oidcToken(), capability: "check_publish", control_budget: control(5)})).rejects.toThrow("broker_control_budget_exhausted");
    expect(fixture.transportRequests).toHaveLength(5);
    expect(fixture.transportRequests[0].kind).toBe("oidc");
  });
  it("funds the cold worker without renewing the original allowance", async () => {
    const fixture = await consumingBroker();
    vi.stubGlobal("fetch", fixture.transport);
    resetOidcJwksCacheForTesting();
    const bound = control(6);
    const result = await fixture.broker.exchange({oidc_token: await fixture.oidcToken(), capability: "check_publish", control_budget: bound});
    expect(result.control_receipt).toEqual({...bound, dispatched: 6});
    expect(fixture.transportRequests).toHaveLength(6);
    expect(result.capability).toBe("check_publish");
  });
});
