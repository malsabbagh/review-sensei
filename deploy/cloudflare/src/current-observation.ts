/** Current authenticated facts only. Never a permission-history epoch or actor role. */
import type { RunAuthority } from "./run-authority";

export interface CurrentObservation {
  schema_version: "current-observation-v1";
  repository: { id: number; full_name: string; owner_id: number; owner_login: string; fork: boolean };
  installation: { id: number; repository_selection: "all" | "selected"; repository_access: true; permissions: Record<string, string> };
  capability: { name: "review_session"; permissions: Record<string, string> };
  actor: { id: number; login: string; source_association: string | null };
}
const object = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);
const exact = (v: Record<string, unknown>, keys: string[]) => Object.keys(v).length === keys.length && keys.every(k => Object.hasOwn(v, k));
const positive = (v: unknown): v is number => typeof v === "number" && Number.isSafeInteger(v) && v > 0;
const login = (v: unknown): v is string => typeof v === "string" && /^[A-Za-z0-9-]{1,100}$/.test(v);
function invalid(): never { throw new Error("broker_current_observation_invalid"); }
export function observationPermissions(value: unknown): Record<string, string> {
  if (!object(value) || Object.keys(value).length < 1 || Object.keys(value).length > 64) invalid();
  const result: Record<string, string> = {};
  for (const key of Object.keys(value).sort()) {
    if (!/^[a-z][a-z0-9_]{0,63}$/.test(key) || !["read", "write"].includes(value[key] as string)) invalid();
    result[key] = value[key] as string;
  }
  return result;
}
export function parseCurrentObservation(value: unknown, authority: RunAuthority): CurrentObservation {
  if (!object(value) || !exact(value, ["schema_version", "repository", "installation", "capability", "actor"]) || value.schema_version !== "current-observation-v1") invalid();
  const r = value.repository, i = value.installation, c = value.capability, a = value.actor;
  if (!object(r) || !exact(r, ["id", "full_name", "owner_id", "owner_login", "fork"]) ||
      !positive(r.id) || r.id !== authority.repository_id || !positive(r.owner_id) || !login(r.owner_login) ||
      typeof r.full_name !== "string" || !/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(r.full_name) || r.full_name.length > 256 ||
      r.full_name.toLowerCase() !== authority.repository.toLowerCase() || r.full_name.split("/")[0].toLowerCase() !== r.owner_login.toLowerCase() || r.fork !== false ||
      !object(i) || !exact(i, ["id", "repository_selection", "repository_access", "permissions"]) || !positive(i.id) ||
      !["all", "selected"].includes(i.repository_selection as string) || i.repository_access !== true ||
      !object(c) || !exact(c, ["name", "permissions"]) || c.name !== "review_session" ||
      !object(a) || !exact(a, ["id", "login", "source_association"]) || !positive(a.id) || !login(a.login)) invalid();
  if (authority.purpose === "review" ? a.source_association !== null :
      authority.actor === null || a.login.toLowerCase() !== authority.actor.login.toLowerCase() || a.source_association !== authority.actor.association) invalid();
  const effective = observationPermissions(c.permissions);
  if (effective.metadata !== "read" || effective.pull_requests !== "write" || Object.keys(effective).some(k => !["metadata", "pull_requests", "contents"].includes(k)) || (effective.contents !== undefined && effective.contents !== "read")) invalid();
  const installed = observationPermissions(i.permissions);
  if (Object.entries(effective).some(([key, level]) => installed[key] !== "write" && installed[key] !== level)) invalid();
  return { schema_version: "current-observation-v1",
    repository: { id: r.id, full_name: r.full_name, owner_id: r.owner_id, owner_login: r.owner_login, fork: false },
    installation: { id: i.id, repository_selection: i.repository_selection as "all" | "selected", repository_access: true, permissions: installed },
    capability: { name: "review_session", permissions: effective },
    actor: { id: a.id, login: a.login, source_association: a.source_association as string | null } };
}
