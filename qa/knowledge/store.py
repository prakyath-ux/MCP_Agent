# qa/knowledge/store.py — Knowledge base persistence, delta detection, and merging

import re
from datetime import datetime
from pathlib import Path

from qa.models.common import TargetApp
from qa.models.knowledge import KnowledgeBase, ScreenKnowledge, L0Element
from qa.models.explore import DeltaReport, DeltaChange


class KnowledgeStore:
    """Load, save, merge, and diff knowledge base files."""

    def __init__(self, base_dir: Path | None = None) -> None:
        self._base = base_dir or Path("artifacts/knowledge")

    def _path_for(self, app: TargetApp) -> Path:
        platform_dir = self._base / app.platform.value
        safe_name = re.sub(r"[^a-z0-9]+", "_", app.app_name.lower()).strip("_")
        return platform_dir / f"{safe_name}.json"

    def save(self, kb: KnowledgeBase) -> Path:
        # Don't overwrite good data with empty knowledge
        path = self._path_for(kb.app)
        if not kb.screens or all(len(s.l0) == 0 for s in kb.screens):
            print(f"  WARNING: Not saving empty knowledge base to {path}")
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        kb.updated_at = datetime.now().isoformat()
        path.write_text(kb.model_dump_json(indent=2))
        return path

    def load(self, app: TargetApp) -> KnowledgeBase | None:
        path = self._path_for(app)
        if not path.exists():
            return None
        return KnowledgeBase.model_validate_json(path.read_text())

    def load_by_name(self, app_name: str, platform: str = "") -> KnowledgeBase | None:
        """Find a knowledge base by app name OR package name (searches all platform dirs)."""
        search = app_name.lower()
        for platform_dir in self._base.iterdir():
            if not platform_dir.is_dir():
                continue
            if platform and platform_dir.name != platform:
                continue
            for f in platform_dir.glob("*.json"):
                try:
                    kb = KnowledgeBase.model_validate_json(f.read_text())
                    # Match by app_name, package_name, or URL
                    if (kb.app.app_name.lower() == search or
                        (kb.app.package_name or "").lower() == search or
                        (kb.app.url or "").lower() == search):
                        return kb
                except Exception:
                    continue
        return None

    def exists(self, app: TargetApp) -> bool:
        return self._path_for(app).exists()

    def compute_delta(self, old: KnowledgeBase, new: KnowledgeBase) -> DeltaReport:
        """Compare two knowledge bases and report what changed."""
        old_ids = {el.element_id for s in old.screens for el in s.l0}
        new_ids = {el.element_id for s in new.screens for el in s.l0}

        added = sorted(new_ids - old_ids)
        removed = sorted(old_ids - new_ids)

        changed: list[DeltaChange] = []
        for eid in old_ids & new_ids:
            old_el = self._find_l0(old, eid)
            new_el = self._find_l0(new, eid)
            if old_el and new_el:
                for field_name in ("type", "required", "behavior", "options", "validation_rules"):
                    old_val = str(getattr(old_el, field_name))
                    new_val = str(getattr(new_el, field_name))
                    if old_val != new_val:
                        changed.append(DeltaChange(
                            element_id=eid,
                            field=field_name,
                            old_value=old_val,
                            new_value=new_val,
                        ))

        return DeltaReport(added=added, removed=removed, changed=changed)

    def merge(self, existing: KnowledgeBase, new: KnowledgeBase) -> KnowledgeBase:
        """Merge new knowledge into existing. Updates L0/L1, preserves L2 history."""
        for new_screen in new.screens:
            old_screen = existing.get_screen(new_screen.screen_name)
            if not old_screen:
                existing.screens.append(new_screen)
                continue

            # Update L0 and L1 from new scan
            old_screen.l0 = new_screen.l0
            old_screen.l1 = new_screen.l1

            # Merge L2: keep old runs, add new ones
            old_l2_map = {el.element_id: el for el in old_screen.l2}
            for new_l2 in new_screen.l2:
                if new_l2.element_id in old_l2_map:
                    old_l2_map[new_l2.element_id].runs.extend(new_l2.runs)
                    old_l2_map[new_l2.element_id].last_seen = new_l2.last_seen
                else:
                    old_screen.l2.append(new_l2)

        existing.updated_at = datetime.now().isoformat()
        existing.version += 1
        return existing

    def _find_l0(self, kb: KnowledgeBase, element_id: str) -> L0Element | None:
        for s in kb.screens:
            for el in s.l0:
                if el.element_id == element_id:
                    return el
        return None
