# Distributing the Enterprise Agent Toolkit extension (EXT-09)

The extension is packaged as a `.vsix` file (`npm run package` in `vscode-extension/`, output `dist/`). The public
Visual Studio Marketplace must not be used for internal tooling, so developers need an internal channel. Options below.

## What the extension ships with

- `ent.updateFeedUrl` setting and an update checker. Once a day (and on the command "Check for Extension Update") it reads
  `updates.json` from the feed, compares `latest` with the installed version, and offers "Install". Install downloads the
  VSIX (or uses the file path) and calls VS Code's `workbench.extensions.installExtension`, then asks for a reload.
- `scripts/publish-private.ps1 -FeedDir <folder>` copies the VSIX into the folder and writes
  `updates.json` = `{"latest": "x.y.z", "vsix": "file.vsix"}`. It never moves `latest` backwards.

This checker is a fallback for channels that have no native auto-update, not a replacement for one.

## Option A: network-share or internal web feed (works today, no new infrastructure)

- Publish: `npm run package; .\scripts\publish-private.ps1 -FeedDir \\fileserver\ent-extensions`.
- Install the first time: `code --install-extension \\fileserver\ent-extensions\enterprise-agent-toolkit-0.1.0.vsix`, then set
  `ent.updateFeedUrl` to the share (push the setting through your device management or a `.vscode/settings.json` in the agent template).
- Updates: the built-in checker. The user clicks "Install" and reloads; it is not silent.
- Limits: not silent; no signature check beyond what the share's access control gives; the setting must be rolled out; the
  share must be reachable (VPN). The same folder can be served over https by any static web server (`ent.updateFeedUrl` accepts a URL).

## Option B: private Open VSX registry

Eclipse Open VSX can be self-hosted (open source). Point VS Code at it with the `extensionsGallery` entry of `product.json`.
- Auto-update: native VS Code auto-update works because the gallery is the registry. Silent and no custom checker needed.
- Limits: VS Code on Windows/macOS is not designed for a custom gallery. Changing `product.json` is unsupported by Microsoft
  and is overwritten on every VS Code update unless managed centrally; the official Microsoft builds have additional
  restrictions on the Marketplace terms. Works well with VS Code forks (VSCodium, Code OSS builds, Cursor-like editors) that
  already default to Open VSX. You operate the server (database, storage, publishing tokens).

## Option C: Visual Studio Marketplace (private extension) / Azure DevOps

Publish with `vsce publish` using a publisher that is not made public; visibility is limited to specific accounts or
organisations (Marketplace "private" visibility, shared with an Azure DevOps organisation).
- Auto-update: native, silent, for users signed in with an account that has access. Best experience on managed Microsoft
  tooling.
- Limits: Marketplace terms and listing rules apply (it is still hosted by Microsoft; check that this is acceptable for your
  data policy); needs an Azure DevOps organisation and a personal access token for publishing; the private visibility
  options and Azure DevOps integration have changed over time, so confirm current behaviour before committing to it.
  Azure Artifacts does not host VS Code extensions (it hosts packages), so it is not an extension feed on its own.

## Recommendation

Start with option A (works now, no approval needed) while the user base is small. Move to B or C when the platform team is
ready to operate or buy a gallery and silent auto-update matters. The extension needs no code change for B or C: set
`ent.updateFeedUrl` empty and let VS Code update it natively.

## Honest limits that apply to all options

- The VSIX is currently unsigned. VS Code verifies signatures only for Marketplace-delivered extensions. For a private feed,
  integrity relies on access control of the feed location. Add a checksum field to `updates.json` and verify it in
  `src/update.ts` if the feed is not trusted.
- Unattended installation across a fleet is a device-management task (for example a script running `code --install-extension`
  or an enterprise policy that allow-lists extension IDs `enterprise.enterprise-agent-toolkit`).
- The publisher id `enterprise` is a placeholder; change `publisher` in `package.json` before publishing anywhere that checks it.
