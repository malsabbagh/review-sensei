import type { WorkerEnv } from "./env";
import {
  SETUP_FILES,
  SETUP_PULL_REQUEST_BODY,
  SETUP_PULL_REQUEST_TITLE,
  SETUP_VARIABLES,
  SETUP_VERSION,
} from "./setup-content";

export const MAX_WEBHOOK_BODY_BYTES = 1024 * 1024;

const MAX_PRIVATE_KEY_BYTES = 64 * 1024;
const MAX_GITHUB_RESPONSE_BYTES = 512 * 1024;
const MAX_SETUP_FILE_BYTES = 128 * 1024;
const JWT_LIFETIME_SECONDS = 9 * 60;
const CLOCK_SKEW_SECONDS = 30;
const SETUP_BRANCH = "review-sensei/setup";
const REPOSITORY_PATTERN = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/;
const DELIVERY_ID_PATTERN = /^[\x21-\x7e]{1,200}$/;
const EVENT_PATTERN = /^[\x21-\x7e]{1,100}$/;
const REQUIRED_SETUP_PERMISSIONS = [
  "contents",
  "pull_requests",
  "variables",
] as const;
const SUPPORTED_EVENTS = new Set(["installation", "installation_repositories"]);
const SETUP_VERSION_PATTERN = /^[ \t]*#[ \t]*ReviewSensei setup version:[ \t]*(\d+)[ \t]*$/m;
const SETUP_VERSION_PREFIX = "ReviewSensei setup version:";

type SetupInspectionState = "absent" | "migration" | "current" | "unknown";

export class WebhookPayloadError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "WebhookPayloadError";
  }
}

export class GitHubSetupError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "GitHubSetupError";
  }
}

export class GitHubSetupTransientError extends GitHubSetupError {
  constructor(message: string) {
    super(message);
    this.name = "GitHubSetupTransientError";
  }
}

export class GitHubAuthConfigurationError extends GitHubSetupError {
  constructor(message: string) {
    super(message);
    this.name = "GitHubAuthConfigurationError";
  }
}

export interface VerifiedDelivery {
  appId: number;
  event: string;
  action: string;
  installationId: number;
  deliveryId: string;
  repository: string | null;
  repositories: string[];
  suspended: boolean;
  permissions: Record<string, string>;
}

export interface SetupResult {
  repository: string;
  status: string;
  pull_request_number?: number;
}

interface JsonObject {
  [key: string]: unknown;
}

interface ApiResponse {
  status: number;
  data: unknown;
}

interface InstallationToken {
  token: string;
  expiresAt: number;
  permissions: Record<string, string>;
}

function isObject(value: unknown): value is JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function positiveInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value > 0;
}

function repositorySlug(value: unknown): value is string {
  return (
    typeof value === "string" &&
    !/[\r\n]/.test(value) &&
    REPOSITORY_PATTERN.test(value)
  );
}

function normalizePermissions(value: unknown): Record<string, string> {
  if (!isObject(value)) {
    return {};
  }
  const permissions: Record<string, string> = {};
  for (const [key, rawValue] of Object.entries(value)) {
    if (typeof rawValue === "string") {
      const normalizedKey = key.trim().toLowerCase().replaceAll("-", "_");
      // GitHub's webhook and installation-token payloads expose the UI's
      // repository "Variables" permission as `actions_variables`.
      const canonicalKey =
        normalizedKey === "actions_variables" ? "variables" : normalizedKey;
      permissions[canonicalKey] = rawValue.trim().toLowerCase();
    }
  }
  return permissions;
}

function hasSetupPermissions(permissions: Record<string, string>): boolean {
  return REQUIRED_SETUP_PERMISSIONS.every(
    (permission) => permissions[permission] === "write",
  );
}

function repositoryPath(repository: string): string {
  const [owner, name] = repository.split("/");
  return `${encodeURIComponent(owner)}/${encodeURIComponent(name)}`;
}

function setupMarkerVersion(content: string): number | null {
  const match = content.match(SETUP_VERSION_PATTERN);
  if (!match) {
    return null;
  }
  const version = Number(match[1]);
  return Number.isSafeInteger(version) ? version : null;
}

function looksLikeLegacySetup(path: string, content: string): boolean {
  if (path === ".github/workflows/review-sensei-review.yml") {
    return [
      "name: ReviewSensei review",
      "review_sensei_version:",
      "prepare-diff",
    ].every((marker) => content.includes(marker));
  }
  if (path === ".github/workflows/review-sensei-uninstall.yml") {
    return [
      "name: Remove ReviewSensei setup",
      "git rm",
      "Remove ReviewSensei setup",
    ].every((marker) => content.includes(marker));
  }
  if (path === ".github/review-sensei/config.yml") {
    return content.includes("provider: ollama") && content.includes("base_url:");
  }
  return false;
}

function classifySetupFiles(
  files: Record<string, string | null>,
): SetupInspectionState {
  const present = Object.entries(files).filter(
    (entry): entry is [string, string] => entry[1] !== null,
  );
  if (present.length === 0) {
    return "absent";
  }

  let hasLegacy = false;
  let hasCurrent = false;
  for (const [path, content] of present) {
    const marker = setupMarkerVersion(content);
    if (content.includes(SETUP_VERSION_PREFIX) && marker === null) {
      return "unknown";
    }
    if (marker !== null) {
      if (marker > SETUP_VERSION) {
        return "unknown";
      }
      if (marker === SETUP_VERSION) {
        hasCurrent = true;
      } else {
        hasLegacy = true;
      }
      continue;
    }
    if (looksLikeLegacySetup(path, content)) {
      hasLegacy = true;
      continue;
    }
    return "unknown";
  }

  if (present.length === SETUP_FILES.length && hasCurrent && !hasLegacy) {
    return "current";
  }
  if (hasLegacy || hasCurrent) {
    return "migration";
  }
  return "unknown";
}

function decodeJsonBody(body: ArrayBuffer): unknown {
  try {
    const text = new TextDecoder("utf-8", { fatal: true }).decode(body);
    return JSON.parse(text) as unknown;
  } catch {
    throw new WebhookPayloadError("GitHub webhook body was invalid JSON");
  }
}

function repositoriesFromPayload(payload: JsonObject): string[] {
  for (const key of ["repositories", "repositories_added"]) {
    const value = payload[key];
    if (!Array.isArray(value)) {
      continue;
    }
    const repositories: string[] = [];
    for (const item of value) {
      if (!isObject(item)) {
        continue;
      }
      const fullName = item.full_name;
      if (typeof fullName === "string") {
        if (!repositorySlug(fullName)) {
          throw new WebhookPayloadError("GitHub webhook repository is invalid");
        }
        repositories.push(fullName);
      }
    }
    if (repositories.length > 0) {
      return repositories;
    }
  }
  return [];
}

/**
 * Parse the already-HMAC-verified request body and retain only setup metadata.
 * Unsupported event names are a successful no-op so GitHub does not retry
 * events that this narrow installation bootstrap does not own.
 */
export function parseVerifiedDelivery(
  body: ArrayBuffer,
  event: string,
  deliveryId: string,
  appId: number,
): VerifiedDelivery | null {
  if (!EVENT_PATTERN.test(event) || /[\r\n]/.test(event)) {
    throw new WebhookPayloadError("GitHub webhook event is invalid");
  }
  if (!DELIVERY_ID_PATTERN.test(deliveryId) || /[\r\n]/.test(deliveryId)) {
    throw new WebhookPayloadError("GitHub webhook delivery id is invalid");
  }
  if (!SUPPORTED_EVENTS.has(event)) {
    return null;
  }

  const payload = decodeJsonBody(body);
  if (!isObject(payload)) {
    throw new WebhookPayloadError("GitHub webhook body must be an object");
  }
  const action = payload.action;
  if (typeof action !== "string" || !action.trim()) {
    throw new WebhookPayloadError("GitHub webhook action is missing");
  }
  const installation = payload.installation;
  if (!isObject(installation) || !positiveInteger(installation.id)) {
    throw new WebhookPayloadError("GitHub webhook installation is invalid");
  }

  const permissions = normalizePermissions(installation.permissions);
  const suspended =
    installation.suspended_at !== null &&
    installation.suspended_at !== undefined;
  let repository: string | null = null;
  let repositories: string[] = [];
  const needsRepository =
    (event === "installation_repositories" && action === "added") ||
    action === "created" ||
    action === "new_permissions_accepted";

  if (needsRepository) {
    repositories = repositoriesFromPayload(payload);
    if (repositories.length === 0) {
      const singleRepository = payload.repository;
      if (isObject(singleRepository) && typeof singleRepository.full_name === "string") {
        if (!repositorySlug(singleRepository.full_name)) {
          throw new WebhookPayloadError("GitHub webhook repository is invalid");
        }
        repository = singleRepository.full_name;
        repositories = [repository];
      }
    }
    if (repositories.length === 0) {
      throw new WebhookPayloadError("GitHub webhook repository is missing");
    }
    repository ??= repositories[0];
  }

  return {
    appId,
    event,
    action,
    installationId: installation.id,
    deliveryId,
    repository,
    repositories,
    suspended,
    permissions,
  };
}

function base64Url(bytes: ArrayBuffer | Uint8Array): string {
  const values = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  let binary = "";
  for (let offset = 0; offset < values.length; offset += 0x8000) {
    binary += String.fromCharCode(...values.subarray(offset, offset + 0x8000));
  }
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
}

function toArrayBuffer(bytes: Uint8Array): ArrayBuffer {
  return bytes.buffer.slice(
    bytes.byteOffset,
    bytes.byteOffset + bytes.byteLength,
  ) as ArrayBuffer;
}

function concatenate(...parts: Uint8Array[]): Uint8Array {
  const length = parts.reduce((total, part) => total + part.byteLength, 0);
  const result = new Uint8Array(length);
  let offset = 0;
  for (const part of parts) {
    result.set(part, offset);
    offset += part.byteLength;
  }
  return result;
}

function derLength(length: number): Uint8Array {
  if (length < 0x80) {
    return Uint8Array.of(length);
  }
  const bytes: number[] = [];
  let remaining = length;
  while (remaining > 0) {
    bytes.unshift(remaining & 0xff);
    remaining >>>= 8;
  }
  return Uint8Array.of(0x80 | bytes.length, ...bytes);
}

function wrapRsaPrivateKey(pkcs1: Uint8Array): Uint8Array {
  // PKCS#8 PrivateKeyInfo wrapper for an RSA PKCS#1 key.
  const version = Uint8Array.of(0x02, 0x01, 0x00);
  const algorithm = Uint8Array.of(
    0x30,
    0x0d,
    0x06,
    0x09,
    0x2a,
    0x86,
    0x48,
    0x86,
    0xf7,
    0x0d,
    0x01,
    0x01,
    0x01,
    0x05,
    0x00,
  );
  const privateKey = concatenate(
    Uint8Array.of(0x04),
    derLength(pkcs1.byteLength),
    pkcs1,
  );
  const sequence = concatenate(version, algorithm, privateKey);
  return concatenate(Uint8Array.of(0x30), derLength(sequence.byteLength), sequence);
}

function decodePrivateKeyPem(value: string): Uint8Array {
  if (!value || new TextEncoder().encode(value).byteLength > MAX_PRIVATE_KEY_BYTES) {
    throw new GitHubAuthConfigurationError("GitHub App private key is unavailable");
  }
  const match = value.match(
    /-----BEGIN (PRIVATE KEY|RSA PRIVATE KEY)-----([\s\S]*?)-----END \1-----/,
  );
  if (!match) {
    throw new GitHubAuthConfigurationError("GitHub App private key is invalid");
  }
  const encoded = match[2].replaceAll(/\s+/g, "");
  if (!encoded || encoded.length % 4 === 1 || !/^[A-Za-z0-9+/]*={0,2}$/.test(encoded)) {
    throw new GitHubAuthConfigurationError("GitHub App private key is invalid");
  }
  try {
    const binary = atob(encoded);
    const der = Uint8Array.from(binary, (character) => character.charCodeAt(0));
    return match[1] === "RSA PRIVATE KEY" ? wrapRsaPrivateKey(der) : der;
  } catch {
    throw new GitHubAuthConfigurationError("GitHub App private key is invalid");
  }
}

async function importPrivateKey(value: string): Promise<CryptoKey> {
  const der = decodePrivateKeyPem(value);
  try {
    return await crypto.subtle.importKey(
      "pkcs8",
      toArrayBuffer(der),
      { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" },
      false,
      ["sign"],
    );
  } catch {
    throw new GitHubAuthConfigurationError("GitHub App private key is invalid");
  }
}

function jsonObject(value: unknown, message: string): JsonObject {
  if (!isObject(value)) {
    throw new GitHubSetupError(message);
  }
  return value;
}

async function readBoundedText(response: Response): Promise<string> {
  const declaredLength = response.headers.get("content-length");
  if (declaredLength !== null) {
    const parsedLength = Number(declaredLength);
    if (!Number.isSafeInteger(parsedLength) || parsedLength < 0 || parsedLength > MAX_GITHUB_RESPONSE_BYTES) {
      throw new GitHubSetupError("GitHub response exceeded the configured size limit");
    }
  }
  if (!response.body) {
    return "";
  }
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {
    while (true) {
      const next = await reader.read();
      if (next.done) {
        break;
      }
      const chunk = next.value;
      total += chunk.byteLength;
      if (total > MAX_GITHUB_RESPONSE_BYTES) {
        await reader.cancel();
        throw new GitHubSetupError("GitHub response exceeded the configured size limit");
      }
      chunks.push(chunk);
    }
  } catch (error) {
    if (error instanceof GitHubSetupError) {
      throw error;
    }
    throw new GitHubSetupTransientError("GitHub response could not be read");
  }
  const bytes = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    throw new GitHubSetupError("GitHub response was not valid UTF-8");
  }
}

function decodeRepositoryFile(data: unknown): string {
  const file = jsonObject(data, "GitHub setup file response was invalid");
  if (file.type !== "file" || file.encoding !== "base64" || file.truncated === true) {
    throw new GitHubSetupError("GitHub setup file response was invalid");
  }
  if (typeof file.content !== "string") {
    throw new GitHubSetupError("GitHub setup file response was invalid");
  }
  const encoded = file.content.replaceAll(/\s+/g, "");
  const maxEncoded = Math.ceil(MAX_SETUP_FILE_BYTES / 3) * 4 + 4;
  if (encoded.length > maxEncoded) {
    throw new GitHubSetupError("GitHub setup file exceeded the configured size limit");
  }
  let binary: string;
  try {
    binary = atob(encoded);
  } catch {
    throw new GitHubSetupError("GitHub setup file response was invalid");
  }
  const bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0));
  if (bytes.byteLength > MAX_SETUP_FILE_BYTES) {
    throw new GitHubSetupError("GitHub setup file exceeded the configured size limit");
  }
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    throw new GitHubSetupError("GitHub setup file was not valid UTF-8");
  }
}

function requireSuccessful(response: ApiResponse): unknown {
  if (response.status >= 200 && response.status < 300) {
    return response.data;
  }
  if (response.status === 429 || response.status >= 500) {
    throw new GitHubSetupTransientError("GitHub request failed temporarily");
  }
  throw new GitHubSetupError("GitHub request was rejected");
}

function pullRequestNumber(value: unknown): number | null {
  if (!isObject(value) || !positiveInteger(value.number)) {
    return null;
  }
  return value.number;
}

export class GitHubSetupService {
  private readonly apiUrl: string;
  private privateKey: Promise<CryptoKey> | undefined;

  constructor(private readonly env: WorkerEnv) {
    const apiUrl = (env.GITHUB_API_URL ?? "https://api.github.com").trim();
    if (
      !/^https:\/\//i.test(apiUrl) &&
      !/^http:\/\/(?:127\.0\.0\.1|localhost)(?::|\/|$)/i.test(apiUrl)
    ) {
      throw new GitHubAuthConfigurationError("GitHub API URL is invalid");
    }
    this.apiUrl = apiUrl.replace(/\/+$/, "");
  }

  async process(delivery: VerifiedDelivery): Promise<SetupResult[]> {
    if (delivery.suspended) {
      return [];
    }
    let selected: string[];
    if (
      delivery.event === "installation" &&
      ["created", "new_permissions_accepted"].includes(delivery.action)
    ) {
      selected = this.selectedRepositories(delivery);
    } else if (
      delivery.event === "installation_repositories" &&
      delivery.action === "added"
    ) {
      selected = this.selectedRepositories(delivery);
    } else {
      return [];
    }

    if (!hasSetupPermissions(delivery.permissions)) {
      return selected.map((repository) => ({
        repository,
        status: "skipped_permissions",
      }));
    }

    const results: SetupResult[] = [];
    const requestedPermissions: Record<string, string> = {
      contents: "write",
      pull_requests: "write",
      actions_variables: "write",
    };
    if (delivery.permissions.workflows === "write") {
      requestedPermissions.workflows = "write";
    }
    for (const repository of selected) {
      const token = await this.installationToken(
        delivery.appId,
        delivery.installationId,
        repository,
        requestedPermissions,
      );
      if (!hasSetupPermissions(token.permissions)) {
        throw new GitHubSetupError("GitHub App installation lacks setup permissions");
      }
      results.push(await this.ensureSetupPullRequest(repository, token.token));
    }
    return results;
  }

  private selectedRepositories(delivery: VerifiedDelivery): string[] {
    const selected = [...delivery.repositories];
    if (delivery.repository && !selected.includes(delivery.repository)) {
      selected.unshift(delivery.repository);
    }
    for (const repository of selected) {
      if (!repositorySlug(repository)) {
        throw new GitHubSetupError("Setup repository must be an owner/repo slug");
      }
    }
    return selected;
  }

  private async appJwt(appId: number): Promise<string> {
    this.privateKey ??= importPrivateKey(this.env.GITHUB_APP_PRIVATE_KEY ?? "");
    const key = await this.privateKey;
    const now = Math.floor(Date.now() / 1000);
    const header = base64Url(new TextEncoder().encode('{"alg":"RS256","typ":"JWT"}'));
    const payload = base64Url(
      new TextEncoder().encode(
        JSON.stringify({
          iat: now - CLOCK_SKEW_SECONDS,
          exp: now + JWT_LIFETIME_SECONDS,
          iss: String(appId),
        }),
      ),
    );
    const input = `${header}.${payload}`;
    let signature: ArrayBuffer;
    try {
      signature = await crypto.subtle.sign(
        "RSASSA-PKCS1-v1_5",
        key,
        new TextEncoder().encode(input),
      );
    } catch {
      throw new GitHubAuthConfigurationError("GitHub App JWT could not be created");
    }
    return `${input}.${base64Url(signature)}`;
  }

  private async installationToken(
    appId: number,
    installationId: number,
    repository: string,
    requestedPermissions: Record<string, string>,
  ): Promise<InstallationToken> {
    const jwt = await this.appJwt(appId);
    const response = await this.request(
      "POST",
      `/app/installations/${installationId}/access_tokens`,
      jwt,
      {
        permissions: requestedPermissions,
        repositories: [repository.split("/")[1]],
      },
    );
    const data = jsonObject(
      requireSuccessful(response),
      "GitHub App token response was invalid",
    );
    const token = data.token;
    const expiresAt = data.expires_at;
    if (typeof token !== "string" || !token.trim() || typeof expiresAt !== "string") {
      throw new GitHubSetupError("GitHub App token response was invalid");
    }
    const expiry = Date.parse(expiresAt);
    if (!Number.isFinite(expiry) || expiry <= Date.now()) {
      throw new GitHubSetupError("GitHub App token response was expired");
    }
    return {
      token,
      expiresAt: expiry,
      permissions: normalizePermissions(data.permissions),
    };
  }

  private async ensureSetupPullRequest(
    repository: string,
    installationToken: string,
  ): Promise<SetupResult> {
    const baseBranch = await this.defaultBranch(repository, installationToken);
    const branchExists = await this.branchExists(
      repository,
      installationToken,
      SETUP_BRANCH,
    );
    const existingBeforeBranch = await this.existingPullRequest(
      repository,
      installationToken,
    );
    if (existingBeforeBranch !== null) {
      return {
        repository,
        status: "skipped_pull_request_exists",
        pull_request_number: existingBeforeBranch,
      };
    }
    const setupState = await this.inspectRepositorySetup(
      repository,
      installationToken,
      baseBranch,
    );
    if (setupState === "current") {
      return { repository, status: "skipped_current" };
    }
    if (setupState === "unknown") {
      return { repository, status: "skipped_unknown_setup" };
    }
    await this.ensureRepositoryVariables(repository, installationToken);
    if (!branchExists || setupState === "migration") {
      await this.createOrUpdateBranch(repository, installationToken, baseBranch);
    }
    const existingAfterBranch = await this.existingPullRequest(
      repository,
      installationToken,
    );
    if (existingAfterBranch !== null) {
      return {
        repository,
        status: "skipped_pull_request_exists",
        pull_request_number: existingAfterBranch,
      };
    }
    const response = await this.request(
      "POST",
      `/repos/${repositoryPath(repository)}/pulls`,
      installationToken,
      {
        title: SETUP_PULL_REQUEST_TITLE,
        head: SETUP_BRANCH,
        base: baseBranch,
        body: SETUP_PULL_REQUEST_BODY,
      },
    );
    if (response.status === 422) {
      // GitHub rejects a second open PR for the same head/base. Re-read the
      // open PR list so that concurrent deliveries remain idempotent.
      const existingAfterCreate = await this.existingPullRequest(
        repository,
        installationToken,
      );
      if (existingAfterCreate !== null) {
        return {
          repository,
          status: "skipped_pull_request_exists",
          pull_request_number: existingAfterCreate,
        };
      }
    }
    const pullRequest = jsonObject(
      requireSuccessful(response),
      "GitHub setup response did not include a pull request",
    );
    const number = pullRequestNumber(pullRequest);
    if (number === null) {
      throw new GitHubSetupError("GitHub setup response did not include a pull request number");
    }
    return { repository, status: "created", pull_request_number: number };
  }

  private async inspectRepositorySetup(
    repository: string,
    token: string,
    baseBranch: string,
  ): Promise<SetupInspectionState> {
    const files: Record<string, string | null> = {};
    for (const file of SETUP_FILES) {
      const encodedPath = file.path
        .split("/")
        .map((segment) => encodeURIComponent(segment))
        .join("/");
      const response = await this.request(
        "GET",
        `/repos/${repositoryPath(repository)}/contents/${encodedPath}?ref=${encodeURIComponent(baseBranch)}`,
        token,
      );
      if (response.status === 404) {
        files[file.path] = null;
        continue;
      }
      files[file.path] = decodeRepositoryFile(requireSuccessful(response));
    }
    return classifySetupFiles(files);
  }

  private async ensureRepositoryVariables(
    repository: string,
    token: string,
  ): Promise<void> {
    const basePath = `/repos/${repositoryPath(repository)}/actions/variables`;
    for (const variable of SETUP_VARIABLES) {
      const variablePath = `${basePath}/${encodeURIComponent(variable.name)}`;
      const existing = await this.request("GET", variablePath, token);
      if (existing.status === 200) {
        continue;
      }
      if (existing.status !== 404) {
        requireSuccessful(existing);
      }
      const created = await this.request("POST", basePath, token, {
        name: variable.name,
        value: variable.value,
      });
      if (created.status === 409) {
        // A concurrent installation delivery may have created the same
        // variable. Re-read it and leave the operator's value untouched.
        const concurrent = await this.request("GET", variablePath, token);
        if (concurrent.status === 200) {
          continue;
        }
      }
      requireSuccessful(created);
    }
  }

  private async defaultBranch(
    repository: string,
    token: string,
  ): Promise<string> {
    const response = await this.request(
      "GET",
      `/repos/${repositoryPath(repository)}`,
      token,
    );
    const data = jsonObject(
      requireSuccessful(response),
      "GitHub setup response was invalid",
    );
    if (typeof data.default_branch !== "string" || !data.default_branch.trim()) {
      throw new GitHubSetupError("GitHub setup response did not include a default branch");
    }
    return data.default_branch;
  }

  private async branchExists(
    repository: string,
    token: string,
    branch: string,
  ): Promise<boolean> {
    const response = await this.request(
      "GET",
      `/repos/${repositoryPath(repository)}/branches/${encodeURIComponent(branch)}`,
      token,
    );
    if (response.status === 404) {
      return false;
    }
    requireSuccessful(response);
    return true;
  }

  private async createOrUpdateBranch(
    repository: string,
    token: string,
    baseBranch: string,
  ): Promise<void> {
    const baseRefResponse = await this.request(
      "GET",
      `/repos/${repositoryPath(repository)}/git/ref/heads/${encodeURIComponent(baseBranch)}`,
      token,
    );
    const baseRef = jsonObject(
      requireSuccessful(baseRefResponse),
      "GitHub setup response did not include a base ref",
    );
    const baseObject = jsonObject(
      baseRef.object,
      "GitHub setup response did not include a base ref",
    );
    if (typeof baseObject.sha !== "string" || !baseObject.sha) {
      throw new GitHubSetupError("GitHub setup response did not include a base commit");
    }

    const treeResponse = await this.request(
      "POST",
      "/repos/" + repositoryPath(repository) + "/git/trees",
      token,
      {
        base_tree: baseObject.sha,
        tree: SETUP_FILES.map((file) => ({
          path: file.path,
          mode: "100644",
          type: "blob",
          content: file.content,
        })),
      },
    );
    const tree = jsonObject(
      requireSuccessful(treeResponse),
      "GitHub setup response did not include a tree",
    );
    if (typeof tree.sha !== "string" || !tree.sha) {
      throw new GitHubSetupError("GitHub setup response did not include a tree");
    }

    const commitResponse = await this.request(
      "POST",
      "/repos/" + repositoryPath(repository) + "/git/commits",
      token,
      {
        message: "Add ReviewSensei review setup files",
        tree: tree.sha,
        parents: [baseObject.sha],
      },
    );
    const commit = jsonObject(
      requireSuccessful(commitResponse),
      "GitHub setup response did not include a commit",
    );
    if (typeof commit.sha !== "string" || !commit.sha) {
      throw new GitHubSetupError("GitHub setup response did not include a commit");
    }

    const refResponse = await this.request(
      "POST",
      "/repos/" + repositoryPath(repository) + "/git/refs",
      token,
      { ref: `refs/heads/${SETUP_BRANCH}`, sha: commit.sha },
    );
    if (refResponse.status === 422) {
      const updateResponse = await this.request(
        "PATCH",
        `/repos/${repositoryPath(repository)}/git/refs/heads/${encodeURIComponent(SETUP_BRANCH)}`,
        token,
        { sha: commit.sha },
      );
      requireSuccessful(updateResponse);
      return;
    }
    requireSuccessful(refResponse);
  }

  private async existingPullRequest(
    repository: string,
    token: string,
  ): Promise<number | null> {
    const owner = repository.split("/", 1)[0];
    const query = new URLSearchParams({
      head: `${owner}:${SETUP_BRANCH}`,
      state: "open",
    });
    const response = await this.request(
      "GET",
      `/repos/${repositoryPath(repository)}/pulls?${query.toString()}`,
      token,
    );
    const data = requireSuccessful(response);
    if (!Array.isArray(data)) {
      throw new GitHubSetupError("GitHub setup response did not include a pull request list");
    }
    for (const item of data) {
      const number = pullRequestNumber(item);
      if (number !== null) {
        return number;
      }
    }
    return null;
  }

  private async request(
    method: string,
    path: string,
    token: string,
    body?: JsonObject,
  ): Promise<ApiResponse> {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 30_000);
    try {
      const response = await fetch(`${this.apiUrl}${path}`, {
        method,
        headers: {
          Accept: "application/vnd.github+json",
          Authorization: `Bearer ${token}`,
          "User-Agent": "ReviewSensei-GitHub-App/1.0 (+https://reviewsensei.dev)",
          "X-GitHub-Api-Version": "2022-11-28",
          ...(body === undefined ? {} : { "Content-Type": "application/json" }),
        },
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: controller.signal,
      });
      const text = await readBoundedText(response);
      let data: unknown = undefined;
      if (text) {
        try {
          data = JSON.parse(text) as unknown;
        } catch {
          throw new GitHubSetupError("GitHub response was invalid JSON");
        }
      }
      return { status: response.status, data };
    } catch (error) {
      if (
        error instanceof GitHubSetupError ||
        error instanceof GitHubSetupTransientError
      ) {
        throw error;
      }
      throw new GitHubSetupTransientError("GitHub request failed");
    } finally {
      clearTimeout(timeout);
    }
  }
}

export async function processDelivery(
  delivery: VerifiedDelivery,
  env: WorkerEnv,
): Promise<SetupResult[]> {
  return new GitHubSetupService(env).process(delivery);
}
