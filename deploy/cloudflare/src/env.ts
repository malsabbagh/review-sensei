export interface WorkerExecution {
  waitUntil(promise: Promise<unknown>): void;
}

export interface WorkerEnv {
  /** Optional platform version-metadata binding, never a source ref or caller value.
   * Not configured by this patch; absent means deployed version is unavailable. */
  CF_VERSION_METADATA?: { id: string };
  GITHUB_APP_ID: string;
  GITHUB_APP_PRIVATE_KEY: string;
  GITHUB_APP_WEBHOOK_SECRET: string;
  /** Install-time update channel resolved before a setup caller is generated. */
  PUBLIC_WORKFLOW_TAG: string;
  /** Optional read token for public workflow tag resolution when anonymous REST quota is exhausted. */
  PUBLIC_WORKFLOW_READ_TOKEN?: string;
  /** Operator admission gate. Absent/open permits; held or invalid values refuse all protected entry. */
  BROKER_ADMISSION_MODE?: string;
  GITHUB_API_URL?: string;
  /** HMAC root for session grants and setup cursors. Absent means configuration_unavailable. */
  REVIEWSENSEI_SIGNING_KEY?: string;
  /** Service binding to this Worker. Setup continuation never uses the public URL. */
  SELF?: Fetcher;
  /** Per-PR v6 coordinator SQLite Durable Object namespace. */
  V6_PR_COORDINATOR?: DurableObjectNamespace;
  /** Comma-separated opt-in owner/repo slugs for v6 pilot isolation. */
  V6_PILOT_REPOSITORIES?: string;
  /** Pinned v6 reusable workflow ref (owner/repo/path@ref). */
  V6_PILOT_WORKFLOW_REF?: string;
  /** Optional validated v6 reusable workflow tag. */
  V6_PILOT_WORKFLOW_TAG?: string;
  /** Comma-separated owner/repo slugs blocked from new v6 lease acquisitions during rollback. */
  V6_PILOT_ROLLBACK_REPOSITORIES?: string;
  /** Clock safety margin in milliseconds for v6 expiry fallback (default 300000). */
  V6_CLOCK_MARGIN_MS?: string;
}

declare global {
  interface Env extends WorkerEnv {}
}
