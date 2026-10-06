"""Per-book spread verification manifest for JP downloads."""

from __future__ import annotations

import hashlib
import ujson as json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MANIFEST_FILENAME = "manifest.json"
STATUS_VERIFIED = "verified"
STATUS_FAILED = "failed"


@dataclass
class SpreadRecord:
    status: str
    sha256: str | None = None
    width: int | None = None
    height: int | None = None
    page_index: int | None = None
    duplicate_of_spread: int | None = None
    failure_reason: str | None = None


@dataclass
class BookManifest:
    book_uuid: str
    total_spreads: int
    jp_force_spread_view: bool
    spreads: dict[int, SpreadRecord] = field(default_factory=dict)

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "book_uuid": self.book_uuid,
            "total_spreads": self.total_spreads,
            "jp_force_spread_view": self.jp_force_spread_view,
            "spreads": {
                str(k): _spread_record_to_dict(v) for k, v in sorted(self.spreads.items())
            },
        }

    @classmethod
    def from_json_dict(cls, data: dict[str, Any]) -> BookManifest:
        spreads_raw = data.get("spreads") or {}
        spreads: dict[int, SpreadRecord] = {}
        for key, entry in spreads_raw.items():
            spreads[int(key)] = _spread_record_from_dict(entry)
        return cls(
            book_uuid=str(data.get("book_uuid", "")),
            total_spreads=int(data.get("total_spreads", 0)),
            jp_force_spread_view=bool(data.get("jp_force_spread_view", False)),
            spreads=spreads,
        )


def _spread_record_to_dict(rec: SpreadRecord) -> dict[str, Any]:
    out: dict[str, Any] = {"status": rec.status}
    if rec.sha256 is not None:
        out["sha256"] = rec.sha256
    if rec.width is not None:
        out["width"] = rec.width
    if rec.height is not None:
        out["height"] = rec.height
    if rec.page_index is not None:
        out["page_index"] = rec.page_index
    if rec.duplicate_of_spread is not None:
        out["duplicate_of_spread"] = rec.duplicate_of_spread
    if rec.failure_reason is not None:
        out["failure_reason"] = rec.failure_reason
    return out


def _spread_record_from_dict(entry: dict[str, Any]) -> SpreadRecord:
    dup = entry.get("duplicate_of_spread")
    return SpreadRecord(
        status=str(entry.get("status", STATUS_FAILED)),
        sha256=entry.get("sha256"),
        width=entry.get("width"),
        height=entry.get("height"),
        page_index=entry.get("page_index"),
        duplicate_of_spread=int(dup) if dup is not None else None,
        failure_reason=entry.get("failure_reason"),
    )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def manifest_path(save_dir: Path) -> Path:
    return save_dir / MANIFEST_FILENAME


def load_manifest(save_dir: Path) -> BookManifest | None:
    path = manifest_path(save_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    return BookManifest.from_json_dict(data)


def save_manifest(save_dir: Path, manifest: BookManifest) -> None:
    path = manifest_path(save_dir)
    text = json.dumps(manifest.to_json_dict(), ensure_ascii=False, indent=2)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def spread_is_verified(manifest: BookManifest | None, spread: int) -> bool:
    if manifest is None:
        return False
    rec = manifest.spreads.get(spread)
    return rec is not None and rec.status == STATUS_VERIFIED


def spread_verified_on_disk(
    manifest: BookManifest | None, spread: int, save_dir: Path
) -> bool:
    if not spread_is_verified(manifest, spread):
        return False
    rec = manifest.spreads[spread]
    path = save_dir / f"page_{spread}.png"
    if not path.is_file():
        return False
    if rec.sha256 and sha256_bytes(path.read_bytes()) != rec.sha256:
        return False
    if rec.duplicate_of_spread is not None:
        return False
    return True


def all_spreads_verified(manifest: BookManifest | None, total_spreads: int) -> bool:
    if manifest is None or manifest.total_spreads != total_spreads:
        return False
    for spread in range(1, total_spreads + 1):
        if not spread_is_verified(manifest, spread):
            return False
    return True


def all_spreads_verified_on_disk(
    manifest: BookManifest | None, total_spreads: int, save_dir: Path
) -> bool:
    if manifest is None or manifest.total_spreads != total_spreads:
        return False
    for spread in range(1, total_spreads + 1):
        if not spread_verified_on_disk(manifest, spread, save_dir):
            return False
    return True


def find_verified_spread_with_hash(
    manifest: BookManifest, spread: int, digest: str
) -> int | None:
    for other, rec in manifest.spreads.items():
        if other == spread:
            continue
        if rec.status == STATUS_VERIFIED and rec.sha256 == digest:
            return other
    return None


def mark_spread_failed(
    manifest: BookManifest, spread: int, reason: str, page_index: int | None = None
) -> None:
    manifest.spreads[spread] = SpreadRecord(
        status=STATUS_FAILED,
        page_index=page_index,
        failure_reason=reason[:500],
    )


def mark_spread_verified(
    manifest: BookManifest,
    spread: int,
    img_bytes: bytes,
    width: int,
    height: int,
    page_index: int,
) -> int | None:
    digest = sha256_bytes(img_bytes)
    duplicate_of = find_verified_spread_with_hash(manifest, spread, digest)
    manifest.spreads[spread] = SpreadRecord(
        status=STATUS_VERIFIED,
        sha256=digest,
        width=width,
        height=height,
        page_index=page_index,
        duplicate_of_spread=duplicate_of,
    )
    return duplicate_of


def collect_incomplete_spreads(manifest: BookManifest, total_spreads: int) -> list[int]:
    missing: list[int] = []
    for spread in range(1, total_spreads + 1):
        if not spread_is_verified(manifest, spread):
            missing.append(spread)
    return missing


def collect_suspect_spreads(manifest: BookManifest) -> list[tuple[int, int]]:
    suspects: list[tuple[int, int]] = []
    for spread, rec in manifest.spreads.items():
        if rec.status == STATUS_VERIFIED and rec.duplicate_of_spread is not None:
            suspects.append((spread, rec.duplicate_of_spread))
    return suspects


def reconcile_manifest_hashes_from_disk(
    manifest: BookManifest, save_dir: Path
) -> int:
    """Align manifest sha256 with on-disk PNG bytes (e.g. after PIL re-save)."""
    updated = 0
    for spread, rec in manifest.spreads.items():
        if rec.status != STATUS_VERIFIED or not rec.sha256:
            continue
        path = save_dir / f"page_{spread}.png"
        if not path.is_file():
            continue
        on_disk = sha256_bytes(path.read_bytes())
        if on_disk != rec.sha256:
            rec.sha256 = on_disk
            updated += 1
    return updated


def spread_resume_skippable(
    manifest: BookManifest | None, spread: int, save_dir: Path
) -> bool:
    """Fast resume check: verified in manifest + non-empty PNG (hash optional)."""
    if not spread_is_verified(manifest, spread):
        return False
    rec = manifest.spreads[spread]
    if rec.duplicate_of_spread is not None:
        return False
    path = save_dir / f"page_{spread}.png"
    try:
        return path.is_file() and path.stat().st_size > 500
    except OSError:
        return False
