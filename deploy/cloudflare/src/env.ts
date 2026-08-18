import type { DeliveryLedger } from "./delivery-ledger";

export interface WorkerEnv {
  GITHUB_APP_ID: string;
  GITHUB_APP_PRIVATE_KEY: string;
  GITHUB_APP_WEBHOOK_SECRET: string;
  GITHUB_API_URL?: string;
  DELIVERY_LEDGER: DurableObjectNamespace<DeliveryLedger>;
}

declare global {
  interface Env extends WorkerEnv {}
}
