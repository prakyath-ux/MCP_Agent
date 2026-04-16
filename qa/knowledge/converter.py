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


# ── Helpers ──────────────────────────────────────────────────────────────────

def _s(d: dict, key: str, default: str = "") -> str:
    """Get string value safely — converts None to default."""
    val = d.get(key, default)
    return val if val is not None else default


def _l(d: dict, key: str) -> list:
    """Get list value safely — converts None to empty list."""
    val = d.get(key, [])
    return val if val is not None else []


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


# ── XPath → CSS conversion (free fallback when LLM doesn't emit css_selector) ──

import re

_CSS_SAFE_ATTRS = (
    "id", "name", "data-testid", "data-test", "data-cy",
    "placeholder", "aria-label", "aria-labelledby",
    "type", "role", "for", "href",
    "title", "alt", "value", "action",
    "class",
)


def xpath_to_css(xpath: str, tag: str | None = None) -> dict[str, str | list[str]]:
    """Parse @name/@id/@placeholder/etc. from an XPath into CSS selectors.

    Returns dict with keys: css_selector, css_fallbacks, and optionally
    js_selector + text_content (for text-matched XPaths like //button[normalize-space()=...]).
    """
    if not xpath:
        return {"css_selector": "", "css_fallbacks": []}

    if tag is None:
        tag_match = re.match(r"//(\w+)", xpath)
        tag = tag_match.group(1) if tag_match else "input"

    attrs = re.findall(r"@([\w-]+)='([^']*)'", xpath)
    if not attrs:
        attrs = re.findall(r'@([\w-]+)="([^"]*)"', xpath)

    selectors = [
        f'{tag}[{name}="{value}"]'
        for name, value in attrs
        if name in _CSS_SAFE_ATTRS
    ]

    if selectors:
        return {"css_selector": selectors[0], "css_fallbacks": selectors[1:]}

    # Text-based XPath fallback — cannot express as pure CSS; emit a JS finder
    text_match = re.search(r'normalize-space\(\)=["\']([^"\']+)["\']', xpath)
    if text_match:
        text = text_match.group(1)
        js = (
            f"[...document.querySelectorAll('{tag}')]"
            f".find(el => el.textContent.trim() === '{text}')"
        )
        return {
            "css_selector": "",
            "css_fallbacks": [],
            "js_selector": js,
            "text_content": text,
        }

    return {"css_selector": "", "css_fallbacks": []}


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
        el_type = _map_type(_s(el, "type", "other"))
        el_name = _s(el, "name") or _s(el, "label") or _s(el, "text")

        if not el_name:
            continue

        eid = make_element_id(screen_name, el_name, el_type.value)

        # L0 — planning index
        options = _l(el, "dropdown_options") if el_type == ElementType.DROPDOWN else []

        l0_elements.append(L0Element(
            element_id=eid,
            name=el_name,
            type=el_type,
            required=bool(el.get("required", False)),
            behavior=_s(el, "behavior"),
            options=options,
            interaction_order=i,
            default_value="",
            validation_rules="",
            screen_name=screen_name,
        ))

        # L1 — execution details
        locators: list[Locator] = []

        # Normalize None → empty string for safety
        label = _s(el, "label") or _s(el, "text")
        identifier = _s(el, "identifier")

        # Label-based locator (most stable for mobile)
        if label:
            locators.append(Locator(strategy="label", value=label, confidence=0.9))

        # Identifier (resource-id)
        if identifier and identifier != "android:id/text1":
            locators.append(Locator(strategy="accessibility_id", value=identifier, confidence=0.95))

        # Coordinates
        center = el.get("center") or {}
        if center:
            locators.append(Locator(
                strategy="coordinates",
                value=f"{center.get('x', 0)},{center.get('y', 0)}",
                confidence=0.7,
            ))

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
        value_entered = _s(el, "value_entered")
        if value_entered:
            runs.append(RunRecord(
                date=_s(meta, "saved_at", datetime.now().isoformat()),
                model="",
                value_entered=value_entered,
                accepted=bool(el.get("accepted", False)),
                issues=_s(el, "issues") or None,
            ))

        now_iso = _s(meta, "saved_at", datetime.now().isoformat())
        l2_elements.append(L2Element(
            element_id=eid,
            runs=runs,
            change_log=[f"{now_iso[:10]}: discovered by explore pipeline"],
            accessibility_issues=_l(data, "accessibility_issues"),
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

    # Accept either the new unified shape ("elements") or the legacy split shape
    # ("fields" + "buttons") from version_2 exports.
    all_elements = (
        _l(data, "elements")
        or (_l(data, "fields") + _l(data, "buttons"))
    )

    for i, el in enumerate(all_elements):
        el_type = _map_type(_s(el, "type", "other"))
        el_name = _s(el, "name")

        if not el_name:
            continue

        eid = make_element_id(screen_name, el_name, el_type.value)

        # L0
        options = _l(el, "dropdown_options")
        l0_elements.append(L0Element(
            element_id=eid,
            name=el_name,
            type=el_type,
            required=bool(el.get("required", False)),
            behavior=_s(el, "behavior"),
            options=options,
            interaction_order=i,
            default_value="",
            validation_rules=_s(el, "validation_rules"),
            screen_name=screen_name,
            accept=_s(el, "accept"),
            semantic_hint=_s(el, "semantic_hint"),
        ))

        # L1 — web uses CSS > xpath > uid. If only xpath is given, derive CSS.
        locators: list[Locator] = []

        css = _s(el, "css_selector")
        css_fallbacks = list(_l(el, "css_fallbacks"))
        js_selector = _s(el, "js_selector")
        xpath = _s(el, "xpath")

        if not css and xpath:
            derived = xpath_to_css(xpath)
            css = str(derived.get("css_selector") or "")
            derived_fallbacks = derived.get("css_fallbacks") or []
            if isinstance(derived_fallbacks, list):
                css_fallbacks.extend(str(fb) for fb in derived_fallbacks)
            js_selector = js_selector or str(derived.get("js_selector") or "")

        if css:
            locators.append(Locator(strategy="css", value=css, confidence=1.0))

        for fb in css_fallbacks:
            if fb:
                locators.append(Locator(strategy="css", value=fb, confidence=0.9))

        if js_selector:
            locators.append(Locator(strategy="js", value=js_selector, confidence=0.85))

        if xpath:
            locators.append(Locator(strategy="xpath", value=xpath, confidence=0.8))

        uid = _s(el, "uid")
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
        value_entered = _s(el, "value_entered")
        if value_entered:
            runs.append(RunRecord(
                date=_s(meta, "saved_at", datetime.now().isoformat()),
                value_entered=value_entered,
                accepted=bool(el.get("accepted", False)),
                issues=_s(el, "issues") or None,
            ))

        now_iso = _s(meta, "saved_at", datetime.now().isoformat())
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
