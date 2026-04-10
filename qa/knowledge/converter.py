# qa/knowledge/converter.py — Convert existing flat knowledge JSONs to L0/L1/L2 format

import json
from datetime import datetime
from pathlib import Path

from qa.models.common import Platform, ElementType, Locator, TargetApp
from qa.models.knowledge import (
    L0Element, L1Element, L2Element, RunRecord,
    ScreenKnowledge, KnowledgeBase,
)
from qa.knowledge.element_id import make_element_id


# ── Element type mapping ─────────────────────────────────────────────────────

_TYPE_MAP = {
    "text_input": ElementType.TEXT_INPUT,
    "text": ElementType.TEXT_INPUT,
    "email": ElementType.TEXT_INPUT,
    "phone": ElementType.TEXT_INPUT,
    "dropdown": ElementType.DROPDOWN,
    "date_picker": ElementType.DATE_PICKER,
    "button": ElementType.BUTTON,
    "file_upload": ElementType.FILE_UPLOAD,
    "checkbox": ElementType.CHECKBOX,
    "radio": ElementType.RADIO,
    "link": ElementType.LINK,
    "nav_tab": ElementType.NAV_TAB,
}


def _map_type(raw_type: str) -> ElementType:
    return _TYPE_MAP.get(raw_type.lower(), ElementType.OTHER)


# ── Convert mobile knowledge JSON ───────────────────────────────────────────

def convert_mobile_knowledge(path: Path) -> KnowledgeBase:
    """Convert a mobile_version/knowledge/*.json to the new KnowledgeBase format."""
    data = json.loads(path.read_text())
    meta = data.get("_meta", {})

    screen_name = meta.get("screen_name", "") or data.get("screen_title", "default")
    package_name = meta.get("package_name", "") or data.get("package_name", "")
    app_name = meta.get("app_name", "") or data.get("screen_title", "Unknown App")
    device_id = data.get("device_id", "")

    app = TargetApp(
        platform=Platform.MOBILE,
        package_name=package_name,
        app_name=app_name,
        device_id=device_id,
    )

    l0_elements: list[L0Element] = []
    l1_elements: list[L1Element] = []
    l2_elements: list[L2Element] = []

    for i, el in enumerate(data.get("elements", [])):
        el_type = _map_type(el.get("type", "other"))
        el_name = el.get("name", "") or el.get("label", "") or el.get("text", "")

        if not el_name:
            continue

        eid = make_element_id(screen_name, el_name, el_type.value)

        # L0 — planning index
        options = []
        if el_type == ElementType.DROPDOWN:
            options = el.get("dropdown_options", [])
            # Some mobile JSONs store behavior text like "opens a list with options: X, Y, Z"
            # We don't parse that here — options come from Pass 1 interaction

        l0_elements.append(L0Element(
            element_id=eid,
            name=el_name,
            type=el_type,
            required=el.get("required", False),
            behavior=el.get("behavior", ""),
            options=options,
            interaction_order=i,
            default_value="",
            validation_rules="",
            screen_name=screen_name,
        ))

        # L1 — execution details
        locators: list[Locator] = []

        # Label-based locator (most stable for mobile)
        label = el.get("label", "") or el.get("text", "")
        if label:
            locators.append(Locator(strategy="label", value=label, confidence=0.9))

        # Identifier (resource-id)
        identifier = el.get("identifier", "")
        if identifier and identifier != "android:id/text1":
            locators.append(Locator(strategy="accessibility_id", value=identifier, confidence=0.95))

        # Coordinates
        center = el.get("center", {})
        if center:
            locators.append(Locator(
                strategy="coordinates",
                value=f"{center.get('x', 0)},{center.get('y', 0)}",
                confidence=0.7,
            ))

        coords = el.get("coordinates", {})
        l1_elements.append(L1Element(
            element_id=eid,
            locators=locators,
            retry_strategy="standard",
            last_known_coordinates=center if center else None,
            widget_type="",
            identifier=identifier,
            screen_name=screen_name,
        ))

        # L2 — evidence
        runs = []
        if el.get("value_entered"):
            runs.append(RunRecord(
                date=meta.get("saved_at", datetime.now().isoformat()),
                model="",
                value_entered=el.get("value_entered", ""),
                accepted=el.get("accepted", False),
                issues=el.get("issues") or None,
            ))

        now_iso = meta.get("saved_at", datetime.now().isoformat())
        l2_elements.append(L2Element(
            element_id=eid,
            runs=runs,
            change_log=[f"{now_iso[:10]}: discovered by explore pipeline"],
            accessibility_issues=data.get("accessibility_issues", []),
            first_seen=now_iso,
            last_seen=now_iso,
        ))

    screen = ScreenKnowledge(
        screen_name=screen_name,
        screen_url=f"{package_name}/{screen_name}",
        l0=l0_elements,
        l1=l1_elements,
        l2=l2_elements,
    )

    return KnowledgeBase(
        app=app,
        screens=[screen],
        created_at=meta.get("saved_at", datetime.now().isoformat()),
        updated_at=datetime.now().isoformat(),
    )


# ── Convert web knowledge JSON ──────────────────────────────────────────────

def convert_web_knowledge(path: Path) -> KnowledgeBase:
    """Convert a version_2/knowledge/*.json to the new KnowledgeBase format."""
    data = json.loads(path.read_text())
    meta = data.get("_meta", {})

    page_url = meta.get("url", "") or data.get("page_url", "")
    app_name = meta.get("app_name", "") or data.get("page_title", "Unknown App")
    screen_name = data.get("page_title", "default")

    app = TargetApp(
        platform=Platform.WEB,
        url=page_url,
        app_name=app_name,
    )

    l0_elements: list[L0Element] = []
    l1_elements: list[L1Element] = []
    l2_elements: list[L2Element] = []

    # Web JSONs have "fields" and "buttons" as separate arrays
    all_elements = data.get("fields", []) + data.get("buttons", [])

    for i, el in enumerate(all_elements):
        el_type = _map_type(el.get("type", "other"))
        el_name = el.get("name", "")

        if not el_name:
            continue

        eid = make_element_id(screen_name, el_name, el_type.value)

        # L0
        options = el.get("dropdown_options", [])
        l0_elements.append(L0Element(
            element_id=eid,
            name=el_name,
            type=el_type,
            required=el.get("required", False),
            behavior=el.get("behavior", ""),
            options=options,
            interaction_order=i,
            default_value="",
            validation_rules=el.get("validation_rules", ""),
            screen_name=screen_name,
        ))

        # L1 — web uses CSS > xpath > uid
        locators: list[Locator] = []

        css = el.get("css_selector", "")
        if css:
            locators.append(Locator(strategy="css", value=css, confidence=1.0))

        fallbacks = el.get("css_fallbacks", [])
        for fb in fallbacks:
            locators.append(Locator(strategy="css", value=fb, confidence=0.9))

        xpath = el.get("xpath", "")
        if xpath:
            locators.append(Locator(strategy="xpath", value=xpath, confidence=0.8))

        uid = el.get("uid", "")
        if uid:
            locators.append(Locator(strategy="uid", value=uid, confidence=0.5))

        l1_elements.append(L1Element(
            element_id=eid,
            locators=locators,
            retry_strategy="standard",
            screen_name=screen_name,
        ))

        # L2
        runs = []
        if el.get("value_entered"):
            runs.append(RunRecord(
                date=meta.get("saved_at", datetime.now().isoformat()),
                value_entered=str(el.get("value_entered", "")),
                accepted=el.get("accepted", False),
                issues=el.get("issues") or None,
            ))

        now_iso = meta.get("saved_at", datetime.now().isoformat())
        l2_elements.append(L2Element(
            element_id=eid,
            runs=runs,
            change_log=[f"{now_iso[:10]}: discovered by explore pipeline"],
            first_seen=now_iso,
            last_seen=now_iso,
        ))

    screen = ScreenKnowledge(
        screen_name=screen_name,
        screen_url=page_url,
        l0=l0_elements,
        l1=l1_elements,
        l2=l2_elements,
    )

    return KnowledgeBase(
        app=app,
        screens=[screen],
        created_at=meta.get("saved_at", datetime.now().isoformat()),
        updated_at=datetime.now().isoformat(),
    )


# ── CLI: Convert all existing knowledge files ────────────────────────────────

def convert_all(project_root: Path) -> list[Path]:
    """Convert all existing knowledge files and save to artifacts/knowledge/."""
    from qa.knowledge.store import KnowledgeStore
    store = KnowledgeStore(project_root / "artifacts" / "knowledge")
    saved: list[Path] = []

    # Mobile knowledge
    mobile_dir = project_root / "mobile_version" / "knowledge"
    if mobile_dir.exists():
        # Group by package name to merge screens into one KB
        kbs_by_package: dict[str, KnowledgeBase] = {}

        for f in sorted(mobile_dir.glob("*.json")):
            kb = convert_mobile_knowledge(f)
            pkg = kb.app.package_name or "unknown"

            if pkg in kbs_by_package:
                kbs_by_package[pkg] = store.merge(kbs_by_package[pkg], kb)
            else:
                kbs_by_package[pkg] = kb

        for kb in kbs_by_package.values():
            path = store.save(kb)
            saved.append(path)
            print(f"  Converted mobile: {path} ({len(kb.screens)} screens)")

    # Web knowledge
    web_dir = project_root / "version_2" / "knowledge"
    if web_dir.exists():
        for f in sorted(web_dir.glob("*.json")):
            kb = convert_web_knowledge(f)
            path = store.save(kb)
            saved.append(path)
            print(f"  Converted web: {path} ({len(kb.screens)} screens)")

    return saved


if __name__ == "__main__":
    project_root = Path(__file__).parent.parent.parent
    print("Converting existing knowledge files...\n")
    saved = convert_all(project_root)
    print(f"\nDone. {len(saved)} knowledge base(s) saved.")
