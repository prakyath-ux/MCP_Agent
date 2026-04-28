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
#         "Employment Status": "PART TIME"
#       },
#       "dependencies": {
#         "additional_details:sector:dropdown": [
#           "additional_details:employment_status:dropdown"
#         ],
#         "additional_details:employment_type:dropdown": [
#           "additional_details:employment_status:dropdown",
#           "additional_details:sector:dropdown"
#         ]
#       }
#     }
#
# Keys can be plain labels or section-qualified ("Section > Label").
# Section-qualified keys always win when both are present.
#
# `dependencies` (Wall 1.1) is a manual override channel for cascaded
# dropdowns where extract hasn't detected the parent → child relationship
# yet. Key is the child element_id; value is an ordered list of parent
# element_ids to fill before enumerating the child. Execute prefers the
# L0 `depends_on` field if populated, and falls back to this sidecar map.

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
    # Wall 1.1 — child element_id → ordered list of parent element_ids
    # that must be filled before the child becomes testable (cascaded
    # dropdowns). Optional override channel for manual tuning until the
    # extract pipeline learns to detect dependencies automatically.
    dependencies: dict[str, list[str]] = field(default_factory=dict)
    # Modal upload metadata — for file_upload fields where the visible
    # trigger opens a modal (TECU "Upload Document" pattern). Maps the
    # field label (plain or section-qualified, same rules as `mapping`)
    # to {doc_type, confirm_label}. Empty values disable that step. Only
    # consulted by wizard / orchestrator paths that pass these through to
    # _upload_file_for_field_impl.
    upload_modes: dict[str, dict[str, str]] = field(default_factory=dict)
    source_path: str = ""

    def get_dependencies(self, element_id: str) -> list[str]:
        """Return the ordered list of parent element_ids this field
        depends on. Empty list = no known dependencies.

        Tolerant to 3-part ↔ 4-part element_id shapes. Extract may or may
        not detect a section heading per field (the same field can be
        3-part on one page and 4-part on another), while the sidecar
        typically uses the canonical 3-part form. Try:
          1. Exact match on the provided id
          2. If the provided id is 4-part, strip the section and retry
          3. If the provided id is 3-part, search for any 4-part key
             with the same (screen, label, type) and return its deps
        """
        if not element_id:
            return []

        def _clean(v: object) -> list[str]:
            if isinstance(v, list):
                return [d for d in v if isinstance(d, str) and d]
            return []

        # 1. exact match
        if element_id in self.dependencies:
            return _clean(self.dependencies[element_id])

        parts = element_id.split(":")
        # 2. 4-part → try canonical 3-part (strip section at index 1)
        if len(parts) == 4:
            alt = f"{parts[0]}:{parts[2]}:{parts[3]}"
            if alt in self.dependencies:
                return _clean(self.dependencies[alt])
        # 3. 3-part → scan sidecar for any 4-part key with same
        #    (screen, label, type)
        if len(parts) == 3:
            screen, label, etype = parts
            for k, v in self.dependencies.items():
                kp = k.split(":")
                if (
                    len(kp) == 4
                    and kp[0] == screen and kp[2] == label and kp[3] == etype
                ):
                    return _clean(v)
        return []

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

    def get_upload_mode(
        self, label: str, section: str = "",
    ) -> tuple[str, str]:
        """Return (doc_type, confirm_label) for a file_upload field.
        Empty strings disable the corresponding modal step. Same lookup
        rules as `get`: section-qualified key wins, case-insensitive
        fallback after exact match."""
        if not label or not self.upload_modes:
            return ("", "")

        candidates = []
        if section:
            candidates.append(f"{section} > {label}")
        candidates.append(label)

        for key in candidates:
            entry = self.upload_modes.get(key)
            if entry is None:
                low = key.lower().strip()
                for k, v in self.upload_modes.items():
                    if k.lower().strip() == low:
                        entry = v
                        break
            if isinstance(entry, dict):
                return (
                    str(entry.get("doc_type", "") or ""),
                    str(entry.get("confirm_label", "") or ""),
                )
        return ("", "")

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

    # Wall 1.1 — optional `dependencies` section: child_element_id → [parent_id, ...]
    deps_raw = data.get("dependencies", {})
    dependencies: dict[str, list[str]] = {}
    if isinstance(deps_raw, dict):
        for child_id, parents in deps_raw.items():
            if not isinstance(child_id, str):
                continue
            if not isinstance(parents, list):
                continue
            clean = [p for p in parents if isinstance(p, str) and p]
            if clean:
                dependencies[child_id] = clean
    elif deps_raw:
        # Non-dict but truthy — user intent was clearly to provide
        # dependencies but the shape is wrong. Fail loud.
        raise ValueError(
            f"Defaults file {resolved} 'dependencies' must be an object "
            f"mapping child element_id → list of parent element_ids."
        )

    # Optional upload_modes — { label_or_section_qualified: {doc_type, confirm_label} }
    upload_modes_raw = data.get("upload_modes", {})
    upload_modes: dict[str, dict[str, str]] = {}
    if isinstance(upload_modes_raw, dict):
        for k, v in upload_modes_raw.items():
            if not isinstance(k, str) or not isinstance(v, dict):
                continue
            entry: dict[str, str] = {}
            for sub in ("doc_type", "confirm_label"):
                val = v.get(sub, "")
                entry[sub] = str(val) if val is not None else ""
            upload_modes[k] = entry
    elif upload_modes_raw:
        raise ValueError(
            f"Defaults file {resolved} 'upload_modes' must be an object "
            f"mapping field label → {{doc_type, confirm_label}}."
        )

    return Defaults(
        app_name=data.get("app_name", app_name),
        mapping=mapping,
        dependencies=dependencies,
        upload_modes=upload_modes,
        source_path=str(resolved),
    )
