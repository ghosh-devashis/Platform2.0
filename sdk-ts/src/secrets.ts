/**
 * Secrets (SDK-05): fetch secrets at runtime from AWS Secrets Manager or SSM Parameter Store.
 *
 * Agents never read secrets from code, env vars or files; they call `secrets.get("name")`. Credentials, region and
 * endpoint come from the standard AWS configuration (`AWS_ENDPOINT_URL=http://localhost:4566` for Floci; unset in
 * AWS), so the same image works everywhere.
 *
 * Fail closed (NFR-03): if a secret can't be fetched, `SecretsError` is thrown. There is no default value.
 * Error messages name the secret but never contain its value.
 */

import { GetParameterCommand, SSMClient } from "@aws-sdk/client-ssm";
import { GetSecretValueCommand, SecretsManagerClient } from "@aws-sdk/client-secrets-manager";
import { getLogger } from "./log.js";

const logger = getLogger("ent_agent_sdk.secrets");

export const SSM_PREFIX = "ssm:";
export const DEFAULT_TTL_SECONDS = 300;

/** Resilience (SDK-16): short timeouts (2 s connect, 5 s read) and up to 3 attempts, then a clear error. */
export const AWS_CLIENT_CONFIG = {
  maxAttempts: 3,
  retryMode: "standard",
  requestHandler: { connectionTimeout: 2000, requestTimeout: 5000 },
} as const;

const NOT_FOUND = new Set(["ResourceNotFoundException", "ParameterNotFound"]);

/** A secret could not be fetched or read. */
export class SecretsError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "SecretsError";
  }
}

/** The secret does not exist (or the caller may not know it exists). */
export class SecretNotFoundError extends SecretsError {
  constructor(message: string) {
    super(message);
    this.name = "SecretNotFoundError";
  }
}

/** Minimal client shape (the AWS SDK clients satisfy it; tests pass stubs). */
export interface SendClient {
  send(command: any): Promise<any>;
}

export interface SecretsClientOptions {
  ttlSeconds?: number;
  /** Inject clients (tests). */
  clients?: { secretsmanager?: SendClient; ssm?: SendClient };
}

export interface GetOptions {
  /** Read one field of a JSON secret. */
  key?: string;
  /** Skip the cache, e.g. after an auth failure. */
  refresh?: boolean;
}

/**
 * Fetches secrets with a time-limited cache. Names are Secrets Manager names/ARNs, or `ssm:/path` for SSM Parameter
 * Store (SecureString decrypted). Cached values expire after `ttlSeconds`; pass `refresh: true` to fetch now.
 */
export class SecretsClient {
  readonly ttlSeconds: number;
  private readonly clients: { secretsmanager?: SendClient; ssm?: SendClient };
  private readonly cache = new Map<string, { value: string; expires: number }>();

  constructor(options: SecretsClientOptions = {}) {
    this.ttlSeconds = options.ttlSeconds ?? DEFAULT_TTL_SECONDS;
    this.clients = { ...(options.clients ?? {}) };
  }

  async get(name: string, options: GetOptions = {}): Promise<string> {
    if (!name) throw new Error("Secret name must not be empty.");
    let value = options.refresh ? undefined : this.cached(name);
    if (value === undefined) {
      value = await this.fetch(name);
      this.cache.set(name, { value, expires: performance.now() + this.ttlSeconds * 1000 });
    }
    return options.key === undefined ? value : field(name, value, options.key);
  }

  clearCache(): void {
    this.cache.clear();
  }

  private cached(name: string): string | undefined {
    const entry = this.cache.get(name);
    return entry && entry.expires > performance.now() ? entry.value : undefined;
  }

  private client(service: "secretsmanager" | "ssm"): SendClient {
    return (this.clients[service] ??=
      service === "ssm" ? new SSMClient({ ...AWS_CLIENT_CONFIG }) : new SecretsManagerClient({ ...AWS_CLIENT_CONFIG }));
  }

  private async fetch(name: string): Promise<string> {
    logger.debug(`Fetching secret ${name}`);
    let response: any;
    try {
      if (name.startsWith(SSM_PREFIX)) {
        response = await this.client("ssm").send(
          new GetParameterCommand({ Name: name.slice(SSM_PREFIX.length), WithDecryption: true }),
        );
        const value = response?.Parameter?.Value;
        if (typeof value !== "string") throw new SecretsError(`Secret '${name}' has no text value.`);
        return value;
      }
      response = await this.client("secretsmanager").send(new GetSecretValueCommand({ SecretId: name }));
    } catch (err: any) {
      if (err instanceof SecretsError) throw err;
      const code: string = err?.name ?? "Unknown";
      if (NOT_FOUND.has(code)) throw new SecretNotFoundError(`Secret '${name}' was not found.`);
      // Service errors carry their code; credential/network/timeout failures carry the error type.
      throw new SecretsError(`Could not fetch secret '${name}' (${code}).`);
    }
    if (typeof response?.SecretString === "string") return response.SecretString;
    try {
      return new TextDecoder("utf-8", { fatal: true }).decode(response.SecretBinary);
    } catch {
      throw new SecretsError(`Secret '${name}' has no text value.`);
    }
  }
}

function field(name: string, value: string, key: string): string {
  let data: unknown;
  try {
    data = JSON.parse(value);
  } catch {
    throw new SecretsError(`Secret '${name}' is not JSON, so key '${key}' can't be read.`);
  }
  if (data === null || typeof data !== "object" || Array.isArray(data) || !(key in data)) {
    throw new SecretsError(`Secret '${name}' has no key '${key}'.`);
  }
  const f = (data as Record<string, unknown>)[key];
  return typeof f === "string" ? f : JSON.stringify(f);
}

let defaultClientInstance: SecretsClient | undefined;

export function defaultClient(): SecretsClient {
  return (defaultClientInstance ??= new SecretsClient());
}

/** Replace the shared client (tests). */
export function setDefaultClient(client: SecretsClient | undefined): void {
  defaultClientInstance = client;
}

/** Fetch a secret using the shared client. See `SecretsClient.get`. */
export function get(name: string, options: GetOptions = {}): Promise<string> {
  return defaultClient().get(name, options);
}

export function clearCache(): void {
  defaultClient().clearCache();
}
