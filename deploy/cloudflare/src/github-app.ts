import type { WorkerEnv } from "./env";
import {
  GitHubApi,
  type GitHubApiResponse,
  type InstallationToken,
  type JsonObject,
} from "./github-api";
import {
  SETUP_FILE_PATHS,
  SETUP_PULL_REQUEST_BODY,
  SETUP_PULL_REQUEST_TITLE,
  SETUP_VARIABLES,
  SETUP_VERSION,
  buildSetupFiles,
  validatePublicWorkflowSha,
} from "./setup-content";

// App JWT signing remains in the shared adapter and uses Web Crypto
// RSASSA-PKCS1-v1_5; setup only consumes its bounded REST seam.

export const MAX_WEBHOOK_BODY_BYTES = 1024 * 1024;

const MAX_SETUP_FILE_BYTES = 128 * 1024;
const SETUP_BRANCH_PREFIX = "review-sensei/setup-v3";
const SETUP_COMMIT_MESSAGE = "Add ReviewSensei review setup files";
const SETUP_APP_LOGIN = "reviewsensei[bot]";
const REPOSITORY_PATTERN = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/;
const DELIVERY_ID_PATTERN = /^[\x21-\x7e]{1,200}$/;
const EVENT_PATTERN = /^[\x21-\x7e]{1,100}$/;
const REQUIRED_SETUP_PERMISSIONS = [
  "contents",
  "pull_requests",
  "variables",
  "workflows",
] as const;
const SUPPORTED_EVENTS = new Set(["installation", "installation_repositories"]);
const SETUP_VERSION_PATTERN = /^[ \t]*#[ \t]*ReviewSensei setup version:[ \t]*(\d+)[ \t]*$/m;
const SETUP_VERSION_PREFIX = "ReviewSensei setup version:";

type SetupInspectionState = "absent" | "migration" | "current" | "unknown";

function setupBranch(baseSha: string, publicWorkflowSha: string): string {
  if (!/^[a-f0-9]{40}$/.test(baseSha) || !/^[a-f0-9]{40}$/.test(publicWorkflowSha)) {
    throw new GitHubSetupError("Setup branch inputs were invalid");
  }
  return `${SETUP_BRANCH_PREFIX}-${baseSha.slice(0, 12)}-${publicWorkflowSha.slice(0, 12)}`;
}

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

const LEGACY_SHA256: Readonly<Record<string, readonly string[]>> = {
  ".github/workflows/review-sensei-review.yml": [
    // Released setup-v2 Worker output.
    "f32527ebe4476eeadc7fc607d2529190a42a4c39820c9c35e3ae3497c1dc15b2",
    // Released setup-v2 Python output.
    "415ae46804c95dc388c4daa8f31db195a4dfba97ab6de03e1837c7f34d4db3d6",
    // Pre-marker Worker and Python outputs released in 8afa49f.
    "f273b9c220a4e1e8336b776f919f7a83b78d9cc6473ad8920f9a7cd1fe250078",
    "bb43f1cf081a46e03fa83baa4906759e202a9b8915c9f38db2cb0cd7ddd79be8",
  ],
  ".github/workflows/review-sensei-uninstall.yml": [
    "350dcf9960e1c325a2ce1e6189ffeb5d993dfc053511791991c67044a0a3edf3",
    "6d330e41a8df5fe7e1a54875091ffc353bbacf6fde727dc28d7bbdc76aedeca0",
  ],
  ".github/review-sensei/config.yml": [
    // Released setup-v2 and pre-marker outputs.
    "a1ebe48445cab35ffde125b7a8a66253d7a007cbed11dc03118c0b9b14d9a58b",
    "d20c350134df752db4d03a67676c5ee244b1f7421650c620e36fa8ecb22e24af",
  ],
};

async function looksLikeLegacySetup(path: string, content: string): Promise<boolean> {
  const expected = LEGACY_SHA256[path];
  if (expected === undefined) {
    return false;
  }
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(content));
  const actual = Array.from(new Uint8Array(digest), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
  return expected.includes(actual);
}

function looksLikeCurrentSetup(
  path: string,
  content: string,
  publicWorkflowSha: string,
): boolean {
  const canonical = buildSetupFiles(publicWorkflowSha).find(
    (file) => file.path === path,
  );
  return canonical?.content === content;
}

async function classifySetupFiles(
  files: Record<string, string | null>,
  publicWorkflowSha: string,
): Promise<SetupInspectionState> {
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
        if (!looksLikeCurrentSetup(path, content, publicWorkflowSha)) {
          return "unknown";
        }
        hasCurrent = true;
      } else {
        if (!(await looksLikeLegacySetup(path, content))) {
          return "unknown";
        }
        hasLegacy = true;
      }
      continue;
    }
    if (await looksLikeLegacySetup(path, content)) {
      hasLegacy = true;
      continue;
    }
    return "unknown";
  }

  if (present.length === SETUP_FILE_PATHS.length && hasCurrent && !hasLegacy) {
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

function repositoriesFromPayload(
  payload: JsonObject,
  key: "repositories" | "repositories_added",
): string[] {
  const value = payload[key];
  if (!Array.isArray(value)) {
    return [];
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
  return repositories;
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
    const payloadKey =
      event === "installation_repositories" && action === "added"
        ? "repositories_added"
        : "repositories";
    repositories = repositoriesFromPayload(payload, payloadKey);
    if (
      repositories.length === 0 &&
      !(event === "installation_repositories" && action === "added")
    ) {
      const singleRepository = payload.repository;
      if (isObject(singleRepository) && typeof singleRepository.full_name === "string") {
        if (!repositorySlug(singleRepository.full_name)) {
          throw new WebhookPayloadError("GitHub webhook repository is invalid");
        }
        repository = singleRepository.full_name;
        repositories = [repository];
      }
    }
    const canListInstallationRepositories =
      event === "installation" &&
      (action === "created" || action === "new_permissions_accepted");
    if (repositories.length === 0 && !canListInstallationRepositories) {
      throw new WebhookPayloadError("GitHub webhook repository is missing");
    }
    repository = repositories.length > 0 ? repositories[0] : null;
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

function jsonObject(value: unknown, message: string): JsonObject {
  if (!isObject(value)) {
    throw new GitHubSetupError(message);
  }
  return value;
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

function requireSuccessful(response: GitHubApiResponse): unknown {
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
  private readonly github: Pick<
    GitHubApi,
    "request" | "installationToken" | "installationRepositories"
  >;

  constructor(
    private readonly env: WorkerEnv,
    github?: Pick<
      GitHubApi,
      "request" | "installationToken" | "installationRepositories"
    >,
  ) {
    try {
      this.github = github ?? new GitHubApi(env);
    } catch (error) {
      throw new GitHubAuthConfigurationError(
        error instanceof Error ? error.message : "GitHub API is unavailable",
      );
    }
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

    if (
      selected.length === 0 &&
      delivery.event === "installation" &&
      ["created", "new_permissions_accepted"].includes(delivery.action)
    ) {
      selected = await this.installationRepositories(delivery.installationId);
    }

    if (!hasSetupPermissions(delivery.permissions)) {
      return selected.map((repository) => ({
        repository,
        status: "skipped_permissions",
      }));
    }

    const publicWorkflowSha = validatePublicWorkflowSha(
      this.env.PUBLIC_WORKFLOW_SHA ?? "",
    );
    const setupFiles = buildSetupFiles(publicWorkflowSha);
    const results: SetupResult[] = [];
    const requestedPermissions: Record<string, string> = {
      contents: "write",
      pull_requests: "write",
      actions_variables: "write",
      workflows: "write",
    };
    for (const repository of selected) {
      const token = await this.installationToken(
        delivery.installationId,
        repository,
        requestedPermissions,
      );
      if (!hasSetupPermissions(token.permissions)) {
        throw new GitHubSetupError("GitHub App installation lacks setup permissions");
      }
      results.push(
        await this.ensureSetupPullRequest(
          repository,
          token.token,
          setupFiles,
          publicWorkflowSha,
        ),
      );
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

  private async installationToken(
    installationId: number,
    repository: string,
    requestedPermissions: Record<string, string>,
  ): Promise<InstallationToken> {
    try {
      return await this.github.installationToken(
        installationId,
        repository,
        requestedPermissions,
      );
    } catch (error) {
      throw new GitHubSetupError(
        error instanceof Error ? error.message : "GitHub App token response was invalid",
      );
    }
  }

  private async installationRepositories(
    installationId: number,
  ): Promise<string[]> {
    try {
      return await this.github.installationRepositories(installationId);
    } catch (error) {
      if (error instanceof GitHubSetupError) {
        throw error;
      }
      throw new GitHubSetupError(
        error instanceof Error
          ? error.message
          : "GitHub installation repositories were unavailable",
      );
    }
  }

  private async ensureSetupPullRequest(
    repository: string,
    installationToken: string,
    setupFiles: readonly { path: string; content: string }[],
    publicWorkflowSha: string,
  ): Promise<SetupResult> {
    const baseBranch = await this.defaultBranch(repository, installationToken);
    const setupState = await this.inspectRepositorySetup(
      repository,
      installationToken,
      baseBranch,
      publicWorkflowSha,
    );
    if (setupState === "current") {
      return { repository, status: "skipped_current" };
    }
    if (setupState === "unknown") {
      return { repository, status: "skipped_unknown_setup" };
    }
    const baseSha = await this.defaultHeadSha(
      repository,
      installationToken,
      baseBranch,
    );
    const branch = setupBranch(baseSha, publicWorkflowSha);
    const branchAlreadyExists = await this.branchExists(
      repository,
      installationToken,
      branch,
    );
    if (branchAlreadyExists && !(await this.isManagedSetupBranch(
      repository,
      installationToken,
      branch,
      baseSha,
      publicWorkflowSha,
    ))) {
      return { repository, status: "skipped_branch_conflict" };
    }
    if (!branchAlreadyExists) {
      const created = await this.createBranch(
        repository,
        installationToken,
        baseSha,
        branch,
        setupFiles,
      );
      if (!created && !(await this.isManagedSetupBranch(
        repository,
        installationToken,
        branch,
        baseSha,
        publicWorkflowSha,
      ))) {
        return { repository, status: "skipped_branch_conflict" };
      }
    }
    await this.ensureRepositoryVariables(repository, installationToken);
    const existingAfterBranch = await this.existingPullRequest(
      repository,
      installationToken,
      branch,
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
        head: branch,
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
        branch,
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
    publicWorkflowSha: string,
  ): Promise<SetupInspectionState> {
    const files: Record<string, string | null> = {};
    for (const path of SETUP_FILE_PATHS) {
      const encodedPath = path
        .split("/")
        .map((segment) => encodeURIComponent(segment))
        .join("/");
      const response = await this.request(
        "GET",
        `/repos/${repositoryPath(repository)}/contents/${encodedPath}?ref=${encodeURIComponent(baseBranch)}`,
        token,
      );
      if (response.status === 404) {
        files[path] = null;
        continue;
      }
      files[path] = decodeRepositoryFile(requireSuccessful(response));
    }
    return await classifySetupFiles(files, publicWorkflowSha);
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

  private async defaultHeadSha(
    repository: string,
    token: string,
    baseBranch: string,
  ): Promise<string> {
    const response = await this.request(
      "GET",
      `/repos/${repositoryPath(repository)}/git/ref/heads/${encodeURIComponent(baseBranch)}`,
      token,
    );
    const data = jsonObject(
      requireSuccessful(response),
      "GitHub setup response did not include a base ref",
    );
    const object = jsonObject(
      data.object,
      "GitHub setup response did not include a base ref",
    );
    if (typeof object.sha !== "string" || !/^[a-f0-9]{40}$/.test(object.sha)) {
      throw new GitHubSetupError("GitHub setup response did not include a base commit");
    }
    return object.sha;
  }

  private async isManagedSetupBranch(
    repository: string,
    token: string,
    branch: string,
    baseSha: string,
    publicWorkflowSha: string,
  ): Promise<boolean> {
    const response = await this.request(
      "GET",
      `/repos/${repositoryPath(repository)}/branches/${encodeURIComponent(branch)}`,
      token,
    );
    const data = jsonObject(
      requireSuccessful(response),
      "GitHub setup branch response was invalid",
    );
    const commit = jsonObject(data.commit, "GitHub setup branch response was invalid");
    const metadata = jsonObject(
      commit.commit,
      "GitHub setup branch response was invalid",
    );
    const sha = commit.sha;
    const parents = commit.parents;
    const author = commit.author;
    if (
      typeof sha !== "string" ||
      !/^[a-f0-9]{40}$/.test(sha) ||
      metadata.message !== SETUP_COMMIT_MESSAGE ||
      !Array.isArray(parents) ||
      parents.length !== 1 ||
      !isObject(parents[0]) ||
      typeof parents[0].sha !== "string" ||
      parents[0].sha !== baseSha ||
      !isObject(author) ||
      author.login !== SETUP_APP_LOGIN ||
      author.type !== "Bot"
    ) {
      return false;
    }
    const comparisonResponse = await this.request(
      "GET",
      `/repos/${repositoryPath(repository)}/compare/${parents[0].sha}...${sha}`,
      token,
    );
    const comparison = jsonObject(
      requireSuccessful(comparisonResponse),
      "GitHub setup branch comparison was invalid",
    );
    if (!Array.isArray(comparison.files) || comparison.files.length === 0) {
      return false;
    }
    const generatedOnly = comparison.files.every(
      (file) =>
        isObject(file) &&
        typeof file.filename === "string" &&
        SETUP_FILE_PATHS.includes(file.filename as (typeof SETUP_FILE_PATHS)[number]) &&
        (file.previous_filename === undefined ||
          (typeof file.previous_filename === "string" &&
            SETUP_FILE_PATHS.includes(
              file.previous_filename as (typeof SETUP_FILE_PATHS)[number],
            ))),
    );
    return generatedOnly &&
      (await this.inspectRepositorySetup(
        repository,
        token,
        branch,
        publicWorkflowSha,
      )) === "current";
  }

  private async createBranch(
    repository: string,
    token: string,
    baseSha: string,
    branch: string,
    setupFiles: readonly { path: string; content: string }[],
  ): Promise<boolean> {
    const treeResponse = await this.request(
      "POST",
      "/repos/" + repositoryPath(repository) + "/git/trees",
      token,
      {
        base_tree: baseSha,
        tree: setupFiles.map((file) => ({
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
        message: SETUP_COMMIT_MESSAGE,
        tree: tree.sha,
        parents: [baseSha],
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
      { ref: `refs/heads/${branch}`, sha: commit.sha },
    );
    if (refResponse.status === 422) {
      return false;
    }
    requireSuccessful(refResponse);
    return true;
  }

  private async existingPullRequest(
    repository: string,
    token: string,
    branch: string,
  ): Promise<number | null> {
    const owner = repository.split("/", 1)[0];
    const query = new URLSearchParams({
      head: `${owner}:${branch}`,
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
  ): Promise<GitHubApiResponse> {
    try {
      return await this.github.request(method, path, token, body);
    } catch (error) {
      if (
        error instanceof GitHubSetupError ||
        error instanceof GitHubSetupTransientError
      ) {
        throw error;
      }
      throw new GitHubSetupTransientError("GitHub request failed");
    }
  }
}

export async function processDelivery(
  delivery: VerifiedDelivery,
  env: WorkerEnv,
): Promise<SetupResult[]> {
  return new GitHubSetupService(env).process(delivery);
}
