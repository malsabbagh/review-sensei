import { brokerDiagnostic } from "./broker-diagnostics";

// These names, not URLs, repositories, tokens, operation IDs or error text, are
// the entire metric dimension vocabulary. Correlation is a log field only.
export const BROKER_STAGES = ["oidc_jwks", "workflow_git", "workflow_ref", "workflow_tag", "installation_token", "github_read", "github_write"] as const;
export type BrokerStage = typeof BROKER_STAGES[number];
type Counters = { attempts: number; admitted: number; dispatched: number; failed: number; timed_out: number; http_error: number; duration_ms: number; last_error?: string };
const MAX_COUNTER = 65_535;
const MAX_DURATION_MS = 60_000;
function bounded(value: number, maximum: number): number {
  return Number.isFinite(value) ? Math.min(maximum, Math.max(0, Math.floor(value))) : 0;
}

/** One synchronous, bounded aggregate log per broker operation; no I/O/retries. */
export class BrokerObservation {
  private readonly started = performance.now();
  private readonly stages: Partial<Record<BrokerStage, Counters>> = {};
  private finished = false;

  constructor(private readonly operation: "exchange" | "session_grant", private readonly correlation?: string) {}

  start(stage: BrokerStage) {
    // Runtime validation keeps even an erroneous future caller from creating
    // unbounded metric labels. Telemetry is never authority or accounting.
    if (!BROKER_STAGES.includes(stage)) return undefined;
    const counters = this.stages[stage] ??= { attempts: 0, admitted: 0, dispatched: 0, failed: 0, timed_out: 0, http_error: 0, duration_ms: 0 };
    const increment = (key: Exclude<keyof Counters, "last_error">) => { counters[key] = Math.min(MAX_COUNTER, counters[key] + 1); };
    increment("attempts");
    const started = performance.now();
    let closed = false;
    return {
      admitted: () => increment("admitted"),
      dispatched: () => increment("dispatched"),
      response: (status: number) => { if (status >= 400 && status <= 599) increment("http_error"); },
      failed: (error: unknown) => {
        increment("failed");
        const code = brokerDiagnostic(error).code;
        counters.last_error = code;
        if (["broker_upstream_timeout", "broker_request_deadline_exhausted", "broker_control_deadline_exhausted"].includes(code)) increment("timed_out");
      },
      close: () => {
        if (closed) return;
        closed = true;
        counters.duration_ms = bounded(counters.duration_ms + bounded(performance.now() - started, MAX_DURATION_MS), MAX_DURATION_MS);
      },
    };
  }

  finish(outcome: "success" | "refused", error?: unknown): void {
    if (this.finished) return;
    this.finished = true;
    const diagnostic = outcome === "success" ? undefined : brokerDiagnostic(error);
    // No asynchronous sink, response body, arbitrary exception, or new service.
    // Logging failure must not replace an authorization result or delay abort.
    try {
      console.info("github_broker_observation", {
        version: 1,
        operation: this.operation,
        outcome,
        code: diagnostic?.code ?? "ok",
        duration_ms: bounded(performance.now() - this.started, MAX_DURATION_MS),
        retries: 0,
        ...(this.correlation !== undefined && /^[a-f0-9]{16,64}-[a-z]{3}$/i.test(this.correlation) ? { correlation_id: this.correlation } : {}),
        stages: Object.fromEntries(Object.entries(this.stages).map(([stage, counters]) => [stage, { ...counters }])),
      });
    } catch { /* Observability must not change broker outcomes. */ }
  }
}
export type BrokerSpan = NonNullable<ReturnType<BrokerObservation["start"]>>;
