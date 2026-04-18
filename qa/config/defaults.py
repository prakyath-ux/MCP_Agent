# qa/config/defaults.py — User-provided valid default values per app.
#
# Pipeline 1 (Extract) uses these to unlock dependent fields (e.g. fill
# Employment Status=PART TIME so Employer becomes enabled).
# Pipeline 3 (Execute) uses these to fill prior-screen fields during the
# setup phase so Execute can reach the target screen to run tests.
#
# Format — artifacts/defaults/{app_name}.json:
#
#     {
#       "app_name": "TECU",
#       "defaults": {
#         "First Name": "JOHN",
#         "Email": "john.doe@example.com",
#         "Mobile Number": "5551234567",
#         "Profile Picture": "profile_picture.png",
#         "Employment Status": "PART TIME",
#         "Marital Status": "SINGLE",
#         "Nominee Details > First Name": "NOMINEE_JOHN"
#       }
#     }
#
# Keys can be plain labels or section-qualified ("Section > Label").
# Section-qualified keys always win when both are present.

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path


DEFAULTS_DIR = Path("artifacts/defaults")


@dataclass
class Defaults:
    """Resolved user-provided defaults for an app. Immutable once loaded."""
    app_name: str = ""
    mapping: dict[str, str] = field(default_factory=dict)
    source_path: str = ""

    def get(self, label: str, section: str = "") -> str | None:
        """Look up a default. Priority:
        1. Exact section-qualified key "Section > Label"
        2. Case-insensitive section-qualified
        3. Exact plain-label match
        4. Case-insensitive plain-label
        Returns None if nothing matches.
        """
        if not label:
            return None

        # 1 — exact section-qualified
        if section:
            qualified = f"{section} > {label}"
            if qualified in self.mapping:
                return self.mapping[qualified]
            # 2 — case-insensitive section-qualified
            lower = qualified.lower().strip()
            for k, v in self.mapping.items():
                if k.lower().strip() == lower:
                    return v

        # 3 — exact plain
        if label in self.mapping:
            return self.mapping[label]

        # 4 — case-insensitive plain (skip section-qualified keys to keep
        # priority clean when label is ambiguous across sections)
        lower = label.lower().strip()
        for k, v in self.mapping.items():
            if ">" in k:
                continue
            if k.lower().strip() == lower:
                return v

        return None

    def has(self, label: str, section: str = "") -> bool:
        return self.get(label, section) is not None

    def summary(self) -> str:
        if not self.mapping:
            return f"Defaults({self.app_name!r}): empty"
        return (
            f"Defaults({self.app_name!r}): {len(self.mapping)} entries "
            f"from {self.source_path or '(none)'}"
        )


def _safe_app_name(app_name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", app_name.lower()).strip("_")


def load_defaults(app_name: str, path: str | Path | None = None) -> Defaults:
    """Load defaults for an app.

    Resolution order:
      1. If `path` is given, load from that file (error if missing/malformed).
      2. Else, load from artifacts/defaults/{sanitized_app_name}.json.
      3. Else, return empty Defaults (no file = no crash).
    """
    explicit = path is not None
    resolved: Path
    if path is not None:
        resolved = Path(path)
    else:
        resolved = DEFAULTS_DIR / f"{_safe_app_name(app_name)}.json"

    if not resolved.exists():
        if explicit:
            raise FileNotFoundError(
                f"Defaults file not found: {resolved}. "
                f"Create it or omit --defaults to use an empty default set."
            )
        return Defaults(app_name=app_name, mapping={}, source_path="")

    try:
        data = json.loads(resolved.read_text())
    except json.JSONDecodeError as e:
        raise ValueError(
            f"Defaults file {resolved} is not valid JSON: {e}"
        ) from e

    mapping_raw = data.get("defaults", {})
    if not isinstance(mapping_raw, dict):
        raise ValueError(
            f"Defaults file {resolved} must have a top-level 'defaults' object."
        )
    # Normalize values to strings — callers rely on this
    mapping: dict[str, str] = {}
    for k, v in mapping_raw.items():
        if not isinstance(k, str):
            continue
        mapping[k] = str(v) if v is not None else ""

    return Defaults(
        app_name=data.get("app_name", app_name),
        mapping=mapping,
        source_path=str(resolved),
    )
