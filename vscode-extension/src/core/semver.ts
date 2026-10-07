export interface Semver {
  major: number;
  minor: number;
  patch: number;
  pre: string;
}

export function parseVersion(v: string): Semver | undefined {
  const m = /^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:-([0-9A-Za-z.-]+))?(?:\+.*)?$/.exec(v.trim());
  if (!m) return undefined;
  return { major: +m[1], minor: +(m[2] ?? 0), patch: +(m[3] ?? 0), pre: m[4] ?? '' };
}

/** Negative if a < b, 0 if equal, positive if a > b. Unparseable versions compare equal. A prerelease sorts below its release. */
export function compareVersions(a: string, b: string): number {
  const x = parseVersion(a);
  const y = parseVersion(b);
  if (!x || !y) return 0;
  for (const k of ['major', 'minor', 'patch'] as const) if (x[k] !== y[k]) return x[k] - y[k];
  if (x.pre === y.pre) return 0;
  if (!x.pre) return 1;
  if (!y.pre) return -1;
  const xs = x.pre.split('.');
  const ys = y.pre.split('.');
  for (let i = 0; i < Math.max(xs.length, ys.length); i++) {
    if (xs[i] === undefined) return -1;
    if (ys[i] === undefined) return 1;
    const xn = /^\d+$/.test(xs[i]);
    const yn = /^\d+$/.test(ys[i]);
    if (xn && yn && +xs[i] !== +ys[i]) return +xs[i] - +ys[i];
    if (xn !== yn) return xn ? -1 : 1;
    if (!xn && xs[i] !== ys[i]) return xs[i] < ys[i] ? -1 : 1;
  }
  return 0;
}

export interface VersionsInfo {
  latest: string;
  supported: { version: string; sunset?: string }[];
}

/** Parse policy/data/versions.json ({"sdk": {"latest", "supported": [...]}}). Returns undefined when unusable. */
export function parseVersionsFile(text: string | undefined): VersionsInfo | undefined {
  if (!text) return undefined;
  try {
    const sdk = (JSON.parse(text) as { sdk?: { latest?: unknown; supported?: unknown } }).sdk;
    if (!sdk || typeof sdk.latest !== 'string' || !parseVersion(sdk.latest)) return undefined;
    const supported = Array.isArray(sdk.supported)
      ? sdk.supported
          .filter((s): s is { version: string; sunset?: string } => !!s && typeof (s as { version?: unknown }).version === 'string')
          .map((s) => ({ version: s.version, sunset: typeof s.sunset === 'string' ? s.sunset : undefined }))
      : [];
    return { latest: sdk.latest, supported };
  } catch {
    return undefined;
  }
}

/** Find the project's ent-agent-sdk version: uv.lock first (exact), then a pin in pyproject.toml. */
export function detectSdkVersion(uvLock: string | undefined, pyproject: string | undefined): { version: string; source: 'uv.lock' | 'pyproject.toml' } | undefined {
  if (uvLock) {
    const m = /name\s*=\s*"ent-agent-sdk"\s*\r?\nversion\s*=\s*"([^"]+)"/.exec(uvLock);
    if (m) return { version: m[1], source: 'uv.lock' };
  }
  if (pyproject) {
    const m = /["']ent-agent-sdk\s*(?:==|~=|>=|===)\s*([0-9][0-9A-Za-z.+-]*)/.exec(pyproject);
    if (m) return { version: m[1], source: 'pyproject.toml' };
  }
  return undefined;
}

export type UpgradeLevel = 'ok' | 'outdated' | 'sunset-soon' | 'unsupported';

export interface UpgradeAssessment {
  level: UpgradeLevel;
  message: string;
}

const DAY = 86_400_000;

export function assessUpgrade(current: string, info: VersionsInfo, now: Date, warnDays = 90): UpgradeAssessment {
  const entry = info.supported.find((s) => compareVersions(s.version, current) === 0);
  if (entry?.sunset) {
    const t = Date.parse(entry.sunset);
    if (!Number.isNaN(t)) {
      if (t < now.getTime()) {
        return { level: 'unsupported', message: `ent-agent-sdk ${current} reached end of support on ${entry.sunset}. Latest is ${info.latest}.` };
      }
      const days = Math.ceil((t - now.getTime()) / DAY);
      if (days <= warnDays) {
        return { level: 'sunset-soon', message: `ent-agent-sdk ${current} loses support on ${entry.sunset} (${days} days). Latest is ${info.latest}.` };
      }
    }
  }
  if (compareVersions(current, info.latest) < 0) {
    return { level: 'outdated', message: `ent-agent-sdk ${current} is behind the latest approved version ${info.latest}.` };
  }
  return { level: 'ok', message: `ent-agent-sdk ${current} is up to date.` };
}
