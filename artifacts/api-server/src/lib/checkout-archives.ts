import { createHash } from "node:crypto";
import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import { basename, resolve } from "node:path";

export type CheckoutArchive = { path: string; filename: string };

const RELEASE_FILENAME = "trustllama-core-backend-v44.zip";
const WEBHOOK_DIRECTORY = "solana-webhook-core-rail-pro";
const WEBHOOK_MANIFEST = "approved-release.json";

export function webhookArchiveIn(
  directory: string,
): CheckoutArchive | null {
  try {
    // The manifest is server-side release approval, not an asset in the web build.
    // Reject missing, malformed, or edited manifests instead of approving arbitrary ZIPs.
    const manifest: unknown = JSON.parse(readFileSync(resolve(directory, WEBHOOK_MANIFEST), "utf8"));
    if (
      !manifest ||
      typeof manifest !== "object" ||
      !("filename" in manifest) ||
      manifest.filename !== "solana-webhook-core-rail-pro.zip" ||
      !("sha256" in manifest) ||
      typeof manifest.sha256 !== "string" ||
      !/^[a-f0-9]{64}$/.test(manifest.sha256)
    ) return null;
    const archives = readdirSync(directory, { withFileTypes: true })
      .filter((entry) =>
        entry.isFile() && /\.(zip|tar|tar\.gz|tgz)$/i.test(entry.name),
      );
    // Never choose between multiple packages or follow a symlink.
    if (archives.length !== 1 || archives[0].name !== manifest.filename) return null;
    const path = resolve(directory, archives[0].name);
    if (statSync(path).size === 0) return null;
    if (createHash("sha256").update(readFileSync(path)).digest("hex") !== manifest.sha256) {
      return null;
    }
    return { path, filename: basename(path) };
  } catch {
    return null;
  }
}

function webhookArchive(): CheckoutArchive | null {
  // Production starts at the workspace root; development starts in artifacts/api-server.
  for (const root of [process.cwd(), resolve(process.cwd(), "../..")]) {
    const archive = webhookArchiveIn(resolve(root, WEBHOOK_DIRECTORY));
    if (archive) return archive;
  }
  return null;
}

function licenseArchive(): CheckoutArchive | null {
  const configured = process.env.LICENSE_ARCHIVE_PATH?.trim();
  const candidates = [
    ...(configured ? [configured] : []),
    resolve(process.cwd(), RELEASE_FILENAME),
    resolve(process.cwd(), "../..", RELEASE_FILENAME),
  ];
  const path = candidates.find((candidate) => existsSync(candidate));
  return path ? { path, filename: RELEASE_FILENAME } : null;
}

export function archiveForTier(tier: string): CheckoutArchive | null {
  if (tier === "webhook_rail_pro") return webhookArchive();
  if (tier === "indie" || tier === "startup" || tier === "enterprise") {
    return licenseArchive();
  }
  return null;
}