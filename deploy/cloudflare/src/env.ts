import type { DeliveryLedger } from "./delivery-ledger";
import type { BrokerLedger } from "./broker-ledger";
import type { SetupContinuationRequest } from "./setup-continuation";

export interface SetupContinuationBinding {
  run(request: SetupContinuationRequest): Promise<void>;
}

export interface WorkerEnv {
  GITHUB_APP_ID: string;
  GITHUB_APP_PRIVATE_KEY: string;
  GITHUB_APP_WEBHOOK_SECRET: string;
  /** Install-time update channel resolved before a setup caller is generated. */
  PUBLIC_WORKFLOW_TAG: string;
  GITHUB_API_URL?: string;
  DELIVERY_LEDGER: DurableObjectNamespace<DeliveryLedger>;
  BROKER_LEDGER: DurableObjectNamespace<BrokerLedger>;
  /**
   * Self-binding used to reconcile one selected repository per invocation.
   * Optional in tests that never schedule setup continuation.
   */
  SETUP_CONTINUATION?: SetupContinuationBinding;
}

declare global {
  interface Env extends WorkerEnv {}
}
