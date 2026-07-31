export type CloudRevision = {
  id: string;
  parent_revision_id: string | null;
  created_at: string;
  device_id: string;
  device_name: string;
  plain_sha256: string;
  encrypted_sha256: string;
  database_size: number;
  snapshot_path: string;
};

export type CloudManifest = {
  version: 1;
  current_revision_id: string;
  updated_at: string;
  revisions: CloudRevision[];
};

export function currentRevision(
  manifest: CloudManifest | null,
): CloudRevision | null {
  if (!manifest) return null;
  return (
    manifest.revisions.find(
      (item) => item.id === manifest.current_revision_id,
    ) ?? null
  );
}

export function appendRevision(
  manifest: CloudManifest | null,
  revision: CloudRevision,
): CloudManifest {
  const revisions = [
    ...(manifest?.revisions ?? []).filter((item) => item.id !== revision.id),
    revision,
  ].slice(-50);
  return {
    version: 1,
    current_revision_id: revision.id,
    updated_at: revision.created_at,
    revisions,
  };
}

export function revisionId(deviceId: string): string {
  const stamp = new Date().toISOString().replace(/[-:.TZ]/g, "");
  const device = deviceId.replace(/[^a-zA-Z0-9]/g, "").slice(0, 8);
  const random = crypto.randomUUID().replace(/-/g, "").slice(0, 8);
  return `${stamp}-${device}-${random}`;
}

export function isManifest(value: unknown): value is CloudManifest {
  if (!value || typeof value !== "object") return false;
  const manifest = value as Partial<CloudManifest>;
  return (
    manifest.version === 1
    && typeof manifest.current_revision_id === "string"
    && Array.isArray(manifest.revisions)
  );
}
