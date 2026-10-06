"""One-off: count spread_verified_on_disk for each babies/ folder."""
from pathlib import Path

from bookphucker.download_manifest import (
    load_manifest,
    reconcile_manifest_hashes_from_disk,
    spread_verified_on_disk,
)

babies = Path("babies")
for save_dir in sorted(babies.iterdir()):
    if not save_dir.is_dir():
        continue
    m = load_manifest(save_dir)
    if m is None:
        print(f"{save_dir.name}: no manifest")
        continue
    reconcile_manifest_hashes_from_disk(m, save_dir)
    ok = sum(
        1
        for s in range(1, m.total_spreads + 1)
        if spread_verified_on_disk(m, s, save_dir)
    )
    print(f"{save_dir.name}: {ok}/{m.total_spreads}")
