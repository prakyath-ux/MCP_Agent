# qa/knowledge/fingerprint_store.py — Wall 2.0: persist AppFingerprint
#
# Analogous to KnowledgeStore but for the per-app selector DNA.
# Stored as <app>.meta.json alongside <app>.json so operators can see
# both the KB and the learned fingerprint in one directory listing.

import os
import re
from pathlib import Path

from qa.models.app_fingerprint import AppFingerprint


def _atomic_write_text(path: Path, content: str) -> None:
    """Temp-file + os.replace. Matches KnowledgeStore's pattern so a
    mid-write crash leaves either the old fingerprint or the new one,
    never a corrupted file."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content)
    os.replace(tmp, path)


def _safe_app_slug(app_name: str) -> str:
    """Mirrors KnowledgeStore._path_for slug logic so <app>.json and
    <app>.meta.json land in the same directory under the same stem."""
    return re.sub(r"[^a-z0-9]+", "_", app_name.lower()).strip("_")


class FingerprintStore:
    """Load and save per-app selector fingerprints.

    Storage layout:
        artifacts/knowledge/web/<app-slug>.json          ← KB (KnowledgeStore)
        artifacts/knowledge/web/<app-slug>.meta.json     ← fingerprint (this)
    """

    def __init__(self, base_dir: Path | None = None) -> None:
        # Mobile doesn't have a fingerprint concept — this store is
        # web-only by design. Base path is the web knowledge dir.
        self._base = base_dir or Path("artifacts/knowledge/web")

    def _path_for(self, app_name: str) -> Path:
        return self._base / f"{_safe_app_slug(app_name)}.meta.json"

    def load(self, app_name: str) -> AppFingerprint | None:
        """Return the stored fingerprint for `app_name`, or None if no
        meta.json exists (never probed). A malformed file returns None
        with a warning print — the probe should be re-run."""
        path = self._path_for(app_name)
        if not path.exists():
            return None
        try:
            return AppFingerprint.model_validate_json(path.read_text())
        except Exception as e:
            print(f"  [fingerprint] ⚠ failed to load {path}: {e} — treating as unprobed")
            return None

    def save(self, fp: AppFingerprint) -> Path:
        """Atomic write of the fingerprint to disk."""
        path = self._path_for(fp.app_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(path, fp.model_dump_json(indent=2))
        return path

    def exists(self, app_name: str) -> bool:
        return self._path_for(app_name).exists()
