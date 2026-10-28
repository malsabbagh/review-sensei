import type { BrokerObservation, BrokerSpan, BrokerStage } from "./broker-observation";
import type { BrokerDispatchControl } from "./broker-dispatch-control";

// Service ceilings, not an allowance, authority proof, or retry policy. Leave
// five seconds below the ordinary client's unchanged thirty-second timeout.
export const BROKER_EXCHANGE_MS = 25_000;
export const BROKER_UPSTREAM_MS = 10_000;
export const BROKER_MAX_DISPATCHES = 60;

export class BrokerLifetime {
  private deadline = Date.now() + BROKER_EXCHANGE_MS;
  private observed = Date.now();
  private readonly controller = new AbortController();
  private timer: ReturnType<typeof setTimeout> | undefined;
  private control: BrokerDispatchControl | undefined;
  private deadlineCode = "broker_request_deadline_exhausted";
  private dispatched = 0;

  constructor(deadlineUnixMs?: number, private readonly observation?: BrokerObservation) {
    if (deadlineUnixMs !== undefined) {
      if (!Number.isSafeInteger(deadlineUnixMs) || deadlineUnixMs <= 0) throw new Error("broker_request_deadline_invalid");
      this.deadline = Math.min(this.deadline, deadlineUnixMs);
    }
  }

  constrain(control: BrokerDispatchControl | undefined): void {
    this.control = control;
    if (control && control.request.deadline_unix_ms <= this.deadline) {
      this.deadline = control.request.deadline_unix_ms;
      this.deadlineCode = "broker_control_deadline_exhausted";
      if (this.timer !== undefined) this.arm();
    }
    this.check();
  }

  check(): number {
    this.control?.check();
    const now = Date.now();
    if (now < this.observed) this.controller.abort(new Error("broker_request_clock_regressed"));
    this.observed = now;
    if (now >= this.deadline) this.controller.abort(new Error(this.deadlineCode));
    if (this.controller.signal.aborted) throw this.controller.signal.reason;
    return this.deadline - now;
  }

  private arm(): void {
    clearTimeout(this.timer);
    this.timer = setTimeout(() => this.controller.abort(new Error(this.deadlineCode)), this.check());
  }

  async run<T>(operation: () => Promise<T>): Promise<T> {
    this.arm();
    try { return await waitFor(operation(), this.controller.signal, () => this.check()); }
    finally {
      clearTimeout(this.timer);
      this.controller.abort(new Error("broker_request_closed"));
    }
  }

  async upstream<T>(operation: (scope: BrokerUpstream) => Promise<T>, control?: BrokerDispatchControl, stage: BrokerStage = "github_read"): Promise<T> {
    const span = this.observation?.start(stage);
    try {
      this.constrain(control);
      if (this.dispatched >= BROKER_MAX_DISPATCHES) throw new Error("broker_request_budget_exhausted");
      const paidSignal = control?.beforeDispatch(); // Exactly one existing debit.
      // Admission is permanent even when the upstream throws or its outcome
      // cannot be observed. Optional client controls can only reduce this cap.
      this.dispatched += 1;
      span?.admitted();
      const scope = new BrokerUpstream(this, this.controller.signal, paidSignal, control, span);
      try { return await scope.wait(operation(scope)); }
      finally { scope.close(); }
    } catch (error) { span?.failed(error); throw error; }
    finally { span?.close(); }
  }
}

function waitFor<T>(pending: Promise<T>, signal: AbortSignal, check: () => unknown): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const abort = () => reject(signal.reason);
    signal.addEventListener("abort", abort, { once: true });
    pending.then(value => {
      signal.removeEventListener("abort", abort);
      try { check(); resolve(value); } catch (error) { reject(error); }
    }, error => { signal.removeEventListener("abort", abort); reject(error); });
    if (signal.aborted) abort();
  });
}

export class BrokerUpstream {
  private readonly controller = new AbortController();
  private timer: ReturnType<typeof setTimeout>;
  private readonly parentAbort = () => this.controller.abort(this.parent.reason);
  private readonly paidAbort = () => this.controller.abort(new Error("broker_control_deadline_exhausted"));

  constructor(private readonly lifetime: BrokerLifetime, private readonly parent: AbortSignal,
    private readonly paid: AbortSignal | undefined, private readonly control?: BrokerDispatchControl, private readonly span?: BrokerSpan) {
    // A refused constructor cannot be closed by its caller. Check before
    // attaching listeners that would retain this otherwise unreachable scope.
    const upstreamDeadline = Date.now() + BROKER_UPSTREAM_MS;
    const remaining = Math.min(BROKER_UPSTREAM_MS, lifetime.check());
    parent.addEventListener("abort", this.parentAbort, { once: true });
    paid?.addEventListener("abort", this.paidAbort, { once: true });
    if (parent.aborted) this.parentAbort();
    if (paid?.aborted) this.paidAbort();
    const timeout = () => {
      if (this.controller.signal.aborted) return;
      // Timer delivery can precede the wall-clock deadline. Re-arm only to
      // the original absolute bounds, without another dispatch or allowance.
      let remaining: number;
      try { remaining = Math.min(upstreamDeadline - Date.now(), lifetime.check()); }
      catch (error) { this.controller.abort(error); return; }
      if (remaining > 0) {
        this.timer = setTimeout(timeout, remaining);
        return;
      }
      this.controller.abort(new Error("broker_upstream_timeout"));
    };
    this.timer = setTimeout(timeout, remaining);
  }

  check(): void {
    this.lifetime.check();
    this.control?.check();
    if (this.controller.signal.aborted) throw this.controller.signal.reason;
  }

  wait<T>(pending: Promise<T>): Promise<T> { return waitFor(pending, this.controller.signal, () => this.check()); }

  async fetch(url: string, init: RequestInit): Promise<Response> {
    this.check();
    this.span?.dispatched();
    const pending = fetch(url, { ...init, signal: this.controller.signal });
    // A transport ignoring abort must not leave a late response body alive.
    void pending.then(response => {
      if (this.controller.signal.aborted) void response.body?.cancel().catch(() => {});
    }, () => {});
    const response = await this.wait(pending);
    this.span?.response(response.status);
    return response;
  }

  async read(reader: ReadableStreamDefaultReader<Uint8Array>): Promise<ReadableStreamReadResult<Uint8Array>> {
    this.check();
    const cancel = () => { void reader.cancel().catch(() => {}); };
    this.controller.signal.addEventListener("abort", cancel, { once: true });
    try { return await this.wait(reader.read()); }
    finally { this.controller.signal.removeEventListener("abort", cancel); }
  }

  close(): void {
    clearTimeout(this.timer);
    this.parent.removeEventListener("abort", this.parentAbort);
    this.paid?.removeEventListener("abort", this.paidAbort);
    this.controller.abort(new Error("broker_upstream_closed"));
  }
}
