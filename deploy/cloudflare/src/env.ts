import type { DeliveryLedger } from "./delivery-ledger";
import type { BrokerLedger } from "./broker-ledger";

export interface WorkerEnv {
  GITHUB_APP_ID: string;
  GITHUB_APP_PRIVATE_KEY: string;
  GITHUB_APP_WEBHOOK_SECRET: string;
  PUBLIC_WORKFLOW_SHA: string;
  GITHUB_API_URL?: string;
  DELIVERY_LEDGER: DurableObjectNamespace<DeliveryLedger>;
  BROKER_LEDGER: DurableObjectNamespace<BrokerLedger>;
}

declare global {
  interface Env extends WorkerEnv {}
}
