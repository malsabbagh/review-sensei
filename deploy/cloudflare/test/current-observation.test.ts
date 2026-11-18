import { describe, expect, it } from "vitest";
import { parseCurrentObservation, observationPermissions } from "../src/current-observation";
import type { RunAuthority } from "../src/run-authority";
const authority = { repository: "acme/widgets", repository_id: 12, purpose: "review", actor: null } as RunAuthority;
function row(): any { return { schema_version: "current-observation-v1",
  repository: { id: 12, full_name: "acme/widgets", owner_id: 23, owner_login: "acme", fork: false },
  installation: { id: 34, repository_selection: "selected", repository_access: true, permissions: { metadata: "read", pull_requests: "write" } },
  capability: { name: "review_session", permissions: { metadata: "read", pull_requests: "write" } },
  actor: { id: 45, login: "octocat", source_association: null } }; }
describe("closed current observations are facts, not permission history", () => {
  it("detaches canonical maps without promoting parser output to authentication", () => {
    const source = row(); const parsed = parseCurrentObservation(source, authority);
    source.installation.permissions.pull_requests = "read";
    source.repository.owner_id = 99;
    expect(parsed.installation.permissions.pull_requests).toBe("write"); expect(parsed.repository.owner_id).toBe(23);
  });
  it.each(["epoch", "actor role", "access false", "unsafe ID", "boolean ID", "unknown field", "wrong scope", "association on review", "missing effective metadata", "unsupported capability"])("rejects %s", mode => {
    const value = row();
    if (mode === "epoch") value.permission_epoch = 1;
    if (mode === "actor role") value.actor.permission = "write";
    if (mode === "access false") value.installation.repository_access = false;
    if (mode === "unsafe ID") value.actor.id = Number.MAX_SAFE_INTEGER + 1;
    if (mode === "boolean ID") value.repository.id = true;
    if (mode === "unknown field") value.installation.complete_history = true;
    if (mode === "wrong scope") value.repository.id = 99;
    if (mode === "association on review") value.actor.source_association = "OWNER";
    if (mode === "missing effective metadata") delete value.capability.permissions.metadata;
    if (mode === "unsupported capability") value.capability.name = "check_publish";
    expect(() => parseCurrentObservation(value, authority)).toThrow();
  });
  it.each([{}, { metadata: "none" }, { "Metadata": "read" }, { "bad-name": "read" }, { metadata: true }, Object.fromEntries(Array.from({length:65}, (_, i) => [`p${i}`, "read"]))])("rejects malformed permission map", value => {
    expect(() => observationPermissions(value)).toThrow();
  });
  it("keeps valid maximum keys and numeric identities bounded", () => {
    expect(Object.keys(observationPermissions(Object.fromEntries(Array.from({length:64}, (_, i) => [`p${i}`, "read"]))))).toHaveLength(64);
    const value = row(); value.actor.id = Number.MAX_SAFE_INTEGER;
    expect(parseCurrentObservation(value, authority).actor.id).toBe(Number.MAX_SAFE_INTEGER);
  });
});
