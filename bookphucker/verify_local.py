"""Offline audit of a babies/ book folder (PNG + manifest)."""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

from bookphucker.download_manifest import (
    STATUS_VERIFIED,
    collect_suspect_spreads,
    load_manifest,
    reconcile_manifest_hashes_from_disk,
    save_manifest,
    sha256_bytes,
    spread_verified_on_disk,
)
from bookphucker.user_log import done, step, warn

_PAGE_RE = re.compile(r"^page_(\d+)\.png$", re.IGNORECASE)


def _list_spread_pngs(save_dir: Path) -> dict[int, Path]:
    pages: dict[int, Path] = {}
    for path in save_dir.iterdir():
        if not path.is_file():
            continue
        m = _PAGE_RE.match(path.name)
        if m:
            pages[int(m.group(1))] = path
    return pages


def audit_book_folder(save_dir: Path) -> int:
    """Print audit report; return 0 if complete, 1 if issues found."""
    if not save_dir.is_dir():
        raise FileNotFoundError(save_dir)

    pages = _list_spread_pngs(save_dir)
    manifest = load_manifest(save_dir)
    exit_code = 0

    step(f"Audit: {save_dir}")
    if pages:
        nums = sorted(pages)
        step(f"PNG files: {len(pages)} (page_{nums[0]} … page_{nums[-1]})")
    else:
        warn("No page_*.png files in folder")
        exit_code = 1

    if manifest is None:
        warn("No manifest.json — on-disk PNGs are not verified")
        exit_code = 1
    else:
        healed = reconcile_manifest_hashes_from_disk(manifest, save_dir)
        if healed:
            save_manifest(save_dir, manifest)
            step(f"Reconciled {healed} manifest hash(es) with on-disk PNGs")
        uuid_label = manifest.book_uuid
        if len(uuid_label) > 8:
            uuid_label = f"{uuid_label[:8]}…"
        step(
            f"Manifest: {manifest.total_spreads} spreads expected, "
            f"book_uuid={uuid_label}"
        )
        verified = sum(
            1
            for s in range(1, manifest.total_spreads + 1)
            if spread_verified_on_disk(manifest, s, save_dir)
        )
        step(f"Verified in manifest: {verified}/{manifest.total_spreads}")
        missing = [
            s
            for s in range(1, manifest.total_spreads + 1)
            if not spread_verified_on_disk(manifest, s, save_dir)
        ]
        if missing:
            warn(f"Missing or failed spreads: {missing[:20]}{'…' if len(missing) > 20 else ''}")
            exit_code = 1
        suspects = collect_suspect_spreads(manifest)
        if suspects:
            warn(f"Manifest duplicate-hash suspects: {suspects[:12]}")
            exit_code = 1

    by_hash: dict[str, list[int]] = defaultdict(list)
    for spread, path in sorted(pages.items()):
        digest = sha256_bytes(path.read_bytes())
        by_hash[digest].append(spread)
        if manifest:
            rec = manifest.spreads.get(spread)
            if rec is None:
                warn(f"page_{spread}.png exists but has no manifest entry (legacy)")
                exit_code = 1
            elif rec.status != STATUS_VERIFIED:
                warn(f"page_{spread}.png on disk but manifest status={rec.status!r}")
                exit_code = 1
            elif rec.sha256 and rec.sha256 != digest:
                warn(f"page_{spread}.png hash differs from manifest")
                exit_code = 1

    dup_groups = [nums for nums in by_hash.values() if len(nums) > 1]
    if dup_groups:
        warn(f"Identical PNG content: {len(dup_groups)} group(s), e.g. {dup_groups[:5]}")
        exit_code = 1

    gaps: list[int] = []
    if pages:
        for n in range(1, max(pages) + 1):
            if n not in pages:
                gaps.append(n)
        if gaps:
            warn(f"Filename gaps in 1..{max(pages)}: {gaps[:20]}{'…' if len(gaps) > 20 else ''}")
            exit_code = 1

    if exit_code == 0:
        done("Audit passed — spreads verified and no duplicate PNG groups")
    return exit_code
