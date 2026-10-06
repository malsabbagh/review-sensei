import catalog from "../../../src/review_sensei/schemas/broker-diagnostics.json";

const reasons: Record<string, { stage: string; action: string; hint: string }> = catalog;

/** Never accept arbitrary exception text, even if it looks like a code. */
export function brokerDiagnostic(error: unknown, correlationId?: string) {
  const message = error instanceof Error && error.message.length <= 160 ? error.message : "";
  const family = /^(github_workflow_tag_unavailable|github_installation_token_failed|github_capability_issue_failed)_([1-5][0-9]{2})$/.exec(message);
  const candidate = family?.[1] ?? message;
  const code = Object.hasOwn(reasons, candidate) ? candidate : "broker_unknown";
  const entry = reasons[code];
  return {
    version: 1,
    code,
    stage: entry.stage,
    action: entry.action,
    ...(family === null ? {} : { upstream_status: Number(family[2]) }),
    ...(correlationId !== undefined && /^[a-f0-9]{16,64}-[a-z]{3}$/i.test(correlationId)
      ? { correlation_id: correlationId } : {}),
  };
}
