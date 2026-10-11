export interface WorkerExecution {
  waitUntil(promise: Promise<unknown>): void;
}

export interface WorkerEnv {
  GITHUB_APP_ID: string;
  GITHUB_APP_PRIVATE_KEY: string;
  GITHUB_APP_WEBHOOK_SECRET: string;
  /** Install-time update channel resolved before a setup caller is generated. */
  PUBLIC_WORKFLOW_TAG: string;
  GITHUB_API_URL?: string;
  /** HMAC root for session grants and setup cursors. Absent means configuration_unavailable. */
  REVIEWSENSEI_SIGNING_KEY?: string;
  /** Service binding to this Worker. Setup continuation never uses the public URL. */
  SELF?: Fetcher;
}

declare global {
  interface Env extends WorkerEnv {}
}
