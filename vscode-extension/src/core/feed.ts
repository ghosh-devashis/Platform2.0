import { compareVersions, parseVersion } from './semver';

export interface UpdateFeed {
  latest: string;
  vsix: string;
}

/** Parse updates.json: {"latest": "x.y.z", "vsix": "file.vsix"}. Throws a readable error on bad input. */
export function parseUpdateFeed(text: string): UpdateFeed {
  let j: { latest?: unknown; vsix?: unknown };
  try {
    j = JSON.parse(text);
  } catch {
    throw new Error('update feed is not valid JSON');
  }
  if (typeof j.latest !== 'string' || !parseVersion(j.latest)) throw new Error('update feed has no valid "latest" version');
  if (typeof j.vsix !== 'string' || !/\.vsix$/i.test(j.vsix) || /(^|[\\/])\.\.([\\/]|$)/.test(j.vsix)) {
    throw new Error('update feed has no valid "vsix" file name');
  }
  return { latest: j.latest, vsix: j.vsix };
}

export function isUpdateAvailable(installed: string, feed: UpdateFeed): boolean {
  return compareVersions(installed, feed.latest) < 0;
}

export const isHttpUrl = (s: string) => /^https?:\/\//i.test(s.trim());

/** Where updates.json lives: the setting as-is if it names a .json file/URL, else <setting>/updates.json. */
export function feedLocation(setting: string): string {
  const s = setting.trim();
  if (/\.json(\?.*)?$/i.test(s)) return s;
  if (isHttpUrl(s)) return s.replace(/\/+$/, '') + '/updates.json';
  return s.replace(/[\\/]+$/, '') + (s.includes('\\') ? '\\' : '/') + 'updates.json';
}

/** Location of the vsix named in the feed, next to the feed itself. */
export function vsixLocation(feedLoc: string, vsix: string): string {
  if (isHttpUrl(vsix)) return vsix;
  if (isHttpUrl(feedLoc)) return new URL(vsix, feedLoc).toString();
  const cut = Math.max(feedLoc.lastIndexOf('\\'), feedLoc.lastIndexOf('/'));
  const dir = cut >= 0 ? feedLoc.slice(0, cut + 1) : '';
  return dir + vsix;
}
