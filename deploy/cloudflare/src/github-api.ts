import type { WorkerEnv } from "./env";

const MAX_RESPONSE_BYTES = 512 * 1024;
const MAX_PRIVATE_KEY_BYTES = 64 * 1024;
const REPOSITORY_PATTERN = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/;

export interface JsonObject {
  [key: string]: unknown;
}

export interface GitHubApiResponse {
  status: number;
  data: unknown;
}

export interface InstallationToken {
  token: string;
  expiresAt: number;
  permissions: Record<string, string>;
}

function isObject(value: unknown): value is JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function base64Url(value: Uint8Array): string {
  let binary = "";
  for (let index = 0; index < value.length; index += 0x8000) {
    binary += String.fromCharCode(...value.subarray(index, index + 0x8000));
  }
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
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

function concat(...parts: Uint8Array[]): Uint8Array {
  const output = new Uint8Array(parts.reduce((total, part) => total + part.byteLength, 0));
  let offset = 0;
  for (const part of parts) {
    output.set(part, offset);
    offset += part.byteLength;
  }
  return output;
}

function wrapRsaPrivateKey(pkcs1: Uint8Array): Uint8Array {
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
  const privateKey = concat(Uint8Array.of(0x04), derLength(pkcs1.byteLength), pkcs1);
  const sequence = concat(version, algorithm, privateKey);
  return concat(Uint8Array.of(0x30), derLength(sequence.byteLength), sequence);
}

function pemBytes(value: string): Uint8Array {
  if (!value || new TextEncoder().encode(value).byteLength > MAX_PRIVATE_KEY_BYTES) {
    throw new Error("github_app_key_invalid");
  }
  const match = value.match(
    /-----BEGIN (PRIVATE KEY|RSA PRIVATE KEY)-----([\s\S]*?)-----END \1-----/,
  );
  if (!match) {
    throw new Error("github_app_key_invalid");
  }
  const encoded = match[2].replaceAll(/\s+/g, "");
  if (
    !encoded ||
    encoded.length % 4 === 1 ||
    !/^[A-Za-z0-9+/]*={0,2}$/.test(encoded)
  ) {
    throw new Error("github_app_key_invalid");
  }
  try {
    const binary = atob(encoded);
    const der = Uint8Array.from(binary, (character) => character.charCodeAt(0));
    return match[1] === "RSA PRIVATE KEY" ? wrapRsaPrivateKey(der) : der;
  } catch {
    throw new Error("github_app_key_invalid");
  }
}

function repositoryPath(repository: string, suffix = ""): string {
  if (!REPOSITORY_PATTERN.test(repository)) {
    throw new Error("github_repository_invalid");
  }
  const [owner, name] = repository.split("/");
  return `/repos/${encodeURIComponent(owner)}/${encodeURIComponent(name)}${suffix}`;
}

async function boundedJson(response: Response): Promise<unknown> {
  const length = response.headers.get("content-length");
  if (length !== null && (!/^\d+$/.test(length) || Number(length) > MAX_RESPONSE_BYTES)) {
    throw new Error("github_response_too_large");
  }
  if (response.body === null) {
    return null;
  }
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  while (true) {
    const next = await reader.read();
    if (next.done) {
      break;
    }
    total += next.value.byteLength;
    if (total > MAX_RESPONSE_BYTES) {
      await reader.cancel();
      throw new Error("github_response_too_large");
    }
    chunks.push(next.value);
  }
  if (total === 0) {
    return null;
  }
  const body = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    body.set(chunk, offset);
    offset += chunk.byteLength;
  }
  try {
    return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(body)) as unknown;
  } catch {
    throw new Error("github_response_invalid");
  }
}

/** Shared bounded App-authenticated GitHub adapter used only by the broker. */
export class GitHubApi {
  private privateKey: Promise<CryptoKey> | undefined;
  private readonly apiUrl: string;

  constructor(private readonly env: WorkerEnv) {
    const apiUrl = (env.GITHUB_API_URL ?? "https://api.github.com").trim();
    if (!/^https:\/\//i.test(apiUrl) && !/^http:\/\/(?:127\.0\.0\.1|localhost)(?::|\/|$)/i.test(apiUrl)) {
      throw new Error("github_api_url_invalid");
    }
    this.apiUrl = apiUrl.replace(/\/+$/, "");
  }

  private async key(): Promise<CryptoKey> {
    this.privateKey ??= crypto.subtle.importKey(
      "pkcs8",
      pemBytes(this.env.GITHUB_APP_PRIVATE_KEY).buffer as ArrayBuffer,
      { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" },
      false,
      ["sign"],
    );
    try {
      return await this.privateKey;
    } catch {
      throw new Error("github_app_key_invalid");
    }
  }

  private async appJwt(): Promise<string> {
    const appId = this.env.GITHUB_APP_ID;
    if (!/^[1-9][0-9]{0,18}$/.test(appId)) {
      throw new Error("github_app_id_invalid");
    }
    const now = Math.floor(Date.now() / 1000);
    const header = base64Url(new TextEncoder().encode('{"alg":"RS256","typ":"JWT"}'));
    const payload = base64Url(
      new TextEncoder().encode(JSON.stringify({ iss: appId, iat: now - 30, exp: now + 540 })),
    );
    const input = `${header}.${payload}`;
    const signature = await crypto.subtle.sign(
      "RSASSA-PKCS1-v1_5",
      await this.key(),
      new TextEncoder().encode(input),
    );
    return `${input}.${base64Url(new Uint8Array(signature))}`;
  }

  async request(
    method: string,
    path: string,
    token: string,
    body?: JsonObject,
  ): Promise<GitHubApiResponse> {
    if (!path.startsWith("/") || /[\r\n]/.test(path)) {
      throw new Error("github_path_invalid");
    }
    const response = await fetch(`${this.apiUrl}${path}`, {
      method,
      headers: {
        accept: "application/vnd.github+json",
        authorization: `Bearer ${token}`,
        "user-agent": "ReviewSensei-GitHub-App/1.0 (+https://reviewsensei.dev)",
        "x-github-api-version": "2022-11-28",
        ...(body === undefined ? {} : { "content-type": "application/json" }),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    return { status: response.status, data: await boundedJson(response) };
  }

  async installationFor(repository: string): Promise<number | null> {
    const response = await this.request("GET", `${repositoryPath(repository)}/installation`, await this.appJwt());
    if (response.status === 404 || response.status === 403) {
      return null;
    }
    if (response.status < 200 || response.status >= 300 || !isObject(response.data)) {
      throw new Error("github_installation_lookup_failed");
    }
    const id = response.data.id;
    return typeof id === "number" && Number.isSafeInteger(id) && id > 0 ? id : null;
  }

  async repositoryInfo(repository: string): Promise<{ id: number; fork: boolean } | null> {
    const response = await this.request("GET", repositoryPath(repository), await this.appJwt());
    if (response.status === 404 || response.status === 403) {
      return null;
    }
    if (response.status < 200 || response.status >= 300 || !isObject(response.data)) {
      throw new Error("github_repository_lookup_failed");
    }
    const id = response.data.id;
    const fork = response.data.fork;
    if (typeof id !== "number" || !Number.isSafeInteger(id) || id <= 0 || typeof fork !== "boolean") {
      throw new Error("github_repository_response_invalid");
    }
    return { id, fork };
  }

  /**
   * List every repository selected for an installation using a bounded,
   * installation-scoped token. The webhook payload for permission acceptance
   * may omit its repository list, so this is the authoritative reconciliation
   * fallback for installation lifecycle events only.
   */
  async installationRepositories(installationId: number): Promise<string[]> {
    if (!Number.isSafeInteger(installationId) || installationId <= 0) {
      throw new Error("github_installation_invalid");
    }
    const tokenResponse = await this.request(
      "POST",
      `/app/installations/${installationId}/access_tokens`,
      await this.appJwt(),
    );
    if (
      tokenResponse.status < 200 ||
      tokenResponse.status >= 300 ||
      !isObject(tokenResponse.data)
    ) {
      throw new Error("github_installation_token_failed");
    }
    const token = tokenResponse.data.token;
    const expiresAt = tokenResponse.data.expires_at;
    if (
      typeof token !== "string" ||
      !token.trim() ||
      typeof expiresAt !== "string" ||
      !Number.isFinite(Date.parse(expiresAt)) ||
      Date.parse(expiresAt) <= Date.now()
    ) {
      throw new Error("github_installation_token_invalid");
    }

    const repositories: string[] = [];
    const seen = new Set<string>();
    for (let page = 1; page <= 10; page += 1) {
      const response = await this.request(
        "GET",
        `/installation/repositories?per_page=100&page=${page}`,
        token,
      );
      if (response.status < 200 || response.status >= 300 || !isObject(response.data)) {
        throw new Error("github_installation_repositories_failed");
      }
      const raw = response.data.repositories;
      if (!Array.isArray(raw)) {
        throw new Error("github_installation_repositories_invalid");
      }
      for (const item of raw) {
        const fullName = isObject(item) ? item.full_name : null;
        if (typeof fullName !== "string" || !REPOSITORY_PATTERN.test(fullName)) {
          throw new Error("github_installation_repository_invalid");
        }
        if (!seen.has(fullName)) {
          seen.add(fullName);
          repositories.push(fullName);
        }
      }
      if (raw.length < 100) {
        return repositories;
      }
    }
    throw new Error("github_installation_repositories_limit");
  }

  /** Issue one short-lived GitHub App installation token for the exact scope. */
  async capabilityToken(
    installationId: number,
    repository: string,
    permissions: Record<string, "read" | "write">,
  ): Promise<string> {
    if (!Number.isSafeInteger(installationId) || installationId <= 0) {
      throw new Error("github_installation_invalid");
    }
    const [, name] = repository.split("/");
    const response = await this.request(
      "POST",
      `/app/installations/${installationId}/access_tokens`,
      await this.appJwt(),
      { repositories: [name], permissions },
    );
    if (response.status < 200 || response.status >= 300 || !isObject(response.data)) {
      throw new Error("github_capability_issue_failed");
    }
    const token = response.data.token;
    const expiresAt = response.data.expires_at;
    const granted = response.data.permissions;
    if (
      typeof token !== "string" ||
      token.length === 0 ||
      typeof expiresAt !== "string" ||
      !Number.isFinite(Date.parse(expiresAt)) ||
      Date.parse(expiresAt) <= Date.now() ||
      !isObject(granted)
    ) {
      throw new Error("github_capability_response_invalid");
    }
    for (const [name, level] of Object.entries(permissions)) {
      if (granted[name] !== level) {
        throw new Error("github_capability_permissions_invalid");
      }
    }
    return token;
  }

  async installationToken(
    installationId: number,
    repository: string,
    permissions: Record<string, string>,
  ): Promise<InstallationToken> {
    if (!Number.isSafeInteger(installationId) || installationId <= 0) {
      throw new Error("github_installation_invalid");
    }
    const [, name] = repository.split("/");
    const response = await this.request(
      "POST",
      `/app/installations/${installationId}/access_tokens`,
      await this.appJwt(),
      { repositories: [name], permissions },
    );
    if (response.status < 200 || response.status >= 300 || !isObject(response.data)) {
      throw new Error("github_installation_token_failed");
    }
    const token = response.data.token;
    const expiresAt = response.data.expires_at;
    const rawPermissions = response.data.permissions;
    if (
      typeof token !== "string" ||
      !token.trim() ||
      typeof expiresAt !== "string" ||
      !Number.isFinite(Date.parse(expiresAt)) ||
      Date.parse(expiresAt) <= Date.now()
    ) {
      throw new Error("github_installation_token_invalid");
    }
    const normalized: Record<string, string> = {};
    if (isObject(rawPermissions)) {
      for (const [key, value] of Object.entries(rawPermissions)) {
        if (typeof value === "string") {
          const normalizedKey = key.trim().toLowerCase().replaceAll("-", "_");
          const canonicalKey =
            normalizedKey === "actions_variables" ? "variables" : normalizedKey;
          normalized[canonicalKey] = value.trim().toLowerCase();
        }
      }
    }
    return {
      token,
      expiresAt: Date.parse(expiresAt),
      permissions: normalized,
    };
  }
}
