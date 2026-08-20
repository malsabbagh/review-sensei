/** Minimal runtime shim for exercising Durable Object classes under Node. */
export class DurableObject<Env> {
  protected readonly ctx: DurableObjectState;
  protected readonly env: Env;

  constructor(ctx: DurableObjectState, env: Env) {
    this.ctx = ctx;
    this.env = env;
  }
}
