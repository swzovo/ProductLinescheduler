import { describe, expect, it, vi } from "vitest";
import {
  appendRevision,
  currentRevision,
  isManifest,
  revisionId,
} from "../cloud/revisions";

describe("云同步版本索引", () => {
  it("追加不可变版本并指向最新版本", () => {
    const first = {
      id: "r1",
      parent_revision_id: null,
      created_at: "2026-07-30T01:00:00Z",
      device_id: "mac",
      device_name: "Mac",
      plain_sha256: "a",
      encrypted_sha256: "b",
      database_size: 100,
      snapshot_path: "revisions/r1.plsync",
    };
    const second = {
      ...first,
      id: "r2",
      parent_revision_id: "r1",
      plain_sha256: "c",
      snapshot_path: "revisions/r2.plsync",
    };
    const firstManifest = appendRevision(null, first);
    const secondManifest = appendRevision(firstManifest, second);

    expect(currentRevision(secondManifest)).toEqual(second);
    expect(secondManifest.revisions.map((item) => item.id)).toEqual(["r1", "r2"]);
    expect(isManifest(secondManifest)).toBe(true);
  });

  it("版本编号包含时间、设备和随机段", () => {
    vi.spyOn(crypto, "randomUUID").mockReturnValue(
      "12345678-1234-1234-1234-123456789abc",
    );
    const id = revisionId("device-abcd");
    expect(id).toMatch(/^\d{17}-deviceab-12345678$/);
  });
});
