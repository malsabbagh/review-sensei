import { describe, expect, it } from "vitest";
import { brokerDiagnostic } from "../src/broker-diagnostics";

const ray = "0123456789abcdef-YYZ";

describe("closed broker diagnostics", () => {
  it("uses an exact allowlist, not a code-shaped text allowlist", () => {
    for (const message of [
      "broker_ghs_sensitive_token", "oidc_repo_private_name", "github_arbitrary_secret",
      "github_capability_issue_failed_500_sensitive", "broker_workflow_rejected\n",
      "__proto__", "constructor", "a".repeat(10000),
    ]) {
      const result = brokerDiagnostic(new Error(message), "ghs_sensitive");
      expect(result).toEqual({ version: 1, code: "broker_unknown", stage: "broker", action: "contact_operator" });
      expect(JSON.stringify(result)).not.toContain(message);
    }
  });

  it("preserves only numeric upstream status from anchored known families", () => {
    expect(brokerDiagnostic(new Error("github_capability_issue_failed_422"), ray)).toEqual({
      version: 1, code: "github_capability_issue_failed", stage: "capability_issuance",
      action: "contact_operator", upstream_status: 422, correlation_id: ray,
    });
    for (const message of ["github_capability_issue_failed_099", "github_capability_issue_failed_600", "github_workflow_tag_unavailable_403_extra"]) {
      expect(brokerDiagnostic(new Error(message)).code).toBe("broker_unknown");
    }
  });

  it("uses broad stages for reasons shared by several checkpoints", () => {
    expect(brokerDiagnostic(new Error("broker_ledger_unavailable")).stage).toBe("broker_ledger");
    expect(brokerDiagnostic(new Error("broker_workflow_rejected")).stage).toBe("workflow_identity");
    expect(brokerDiagnostic(new Error("broker_session_grant_invalid")).action).toBe("check_session");
  });
});
