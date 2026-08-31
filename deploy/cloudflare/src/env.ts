import type { DeliveryLedger } from "./delivery-ledger";
import type { BrokerLedger } from "./broker-ledger";

export interface WorkerEnv {
  GITHUB_APP_ID: string;
  GITHUB_APP_PRIVATE_KEY: string;
  GITHUB_APP_WEBHOOK_SECRET: string;
  /** Install-time update channel resolved before a setup caller is generated. */
  PUBLIC_WORKFLOW_TAG: string;
  /** Commit resolved from PUBLIC_WORKFLOW_TAG; generated callers and broker use this pin. */
  PUBLIC_WORKFLOW_SHA: string;
  /** Comma-separated older pinned workflow SHAs retained during migration/rollback. */
  PUBLIC_WORKFLOW_LEGACY_SHAS?: string;
  GITHUB_API_URL?: string;
  DELIVERY_LEDGER: DurableObjectNamespace<DeliveryLedger>;
  BROKER_LEDGER: DurableObjectNamespace<BrokerLedger>;
}

declare global {
  interface Env extends WorkerEnv {}
}
