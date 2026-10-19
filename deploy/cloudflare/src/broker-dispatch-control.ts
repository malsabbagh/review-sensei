/** Request-owned, downward-only physical broker subrequest admission.
 * The caller prepays the whole liability; successful or uncertain work never
 * refunds it. A receipt reports observations, not a fresh allowance or authority.
 */
export interface BrokerControlRequest {
  schema_version: "1.0";
  operation_id: string;
  request_id: string;
  deadline_unix_ms: number;
  max_dispatches: number;
}

export interface BrokerControlReceipt extends BrokerControlRequest {
  dispatched: number;
}

export class BrokerDispatchControl {
  readonly request: BrokerControlRequest;
  private observed: number;
  private dispatched = 0;

  constructor(value: unknown) {
    const now = Date.now();
    if (typeof value !== "object" || value === null || Array.isArray(value)) throw new Error("broker_control_invalid");
    const data = value as Record<string, unknown>;
    const keys = ["schema_version", "operation_id", "request_id", "deadline_unix_ms", "max_dispatches"];
    if (Object.keys(data).length !== keys.length || keys.some(key => !Object.hasOwn(data, key)) ||
      data.schema_version !== "1.0" ||
      typeof data.operation_id !== "string" || !/^[a-f0-9]{64}$/.test(data.operation_id) ||
      typeof data.request_id !== "string" || !/^[a-f0-9]{64}$/.test(data.request_id) ||
      typeof data.deadline_unix_ms !== "number" || !Number.isSafeInteger(data.deadline_unix_ms) ||
      data.deadline_unix_ms <= now || data.deadline_unix_ms > now + 60_000 ||
      typeof data.max_dispatches !== "number" || !Number.isSafeInteger(data.max_dispatches) ||
      data.max_dispatches < 1 || data.max_dispatches > 60
    ) throw new Error("broker_control_invalid");
    this.request = Object.freeze({
      schema_version: "1.0", operation_id: data.operation_id,
      request_id: data.request_id, deadline_unix_ms: data.deadline_unix_ms,
      max_dispatches: data.max_dispatches,
    });
    this.observed = now;
  }

  check(): number {
    const now = Date.now();
    if (now < this.observed) throw new Error("broker_control_clock_regressed");
    this.observed = now;
    const remaining = this.request.deadline_unix_ms - now;
    if (remaining <= 0) throw new Error("broker_control_deadline_exhausted");
    return remaining;
  }

  beforeDispatch(): AbortSignal {
    const remaining = this.check();
    if (this.dispatched >= this.request.max_dispatches) throw new Error("broker_control_budget_exhausted");
    this.dispatched += 1;
    return AbortSignal.timeout(remaining);
  }

  async waitFor<T>(pending: Promise<T>): Promise<T> {
    // A shared JWKS fetch belongs to its original request. Bound this wait
    // without canceling somebody else's I/O or charging a nonexistent fetch.
    const signal = AbortSignal.timeout(this.check());
    return await new Promise<T>((resolve, reject) => {
      const abort = () => reject(new Error("broker_control_deadline_exhausted"));
      signal.addEventListener("abort", abort, { once: true });
      pending.then(value => {
        signal.removeEventListener("abort", abort);
        try { this.check(); resolve(value); } catch (error) { reject(error); }
      }, error => {
        signal.removeEventListener("abort", abort);
        reject(error);
      });
    });
  }

  receipt(): BrokerControlReceipt {
    this.check();
    return { ...this.request, dispatched: this.dispatched };
  }
}
