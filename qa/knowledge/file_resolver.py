# qa/knowledge/file_resolver.py — Resolve test file paths for file_upload
# elements using a 4-tier lookup: per-app registry → per-app folder match →
# global semantic default → accept-attribute fallback.

import json
import re
from pathlib import Path


# Default hint → global filename mapping. Add new hints here as apps introduce them.
_SEMANTIC_DEFAULTS: dict[str, str] = {
    "profile_picture": "dummy.jpg",
    "id_document": "dummy.jpg",
    "signature": "dummy.png",
    "proof_of_address": "dummy.pdf",
    "bank_statement": "dummy.pdf",
    "contract": "dummy.pdf",
    "other": "dummy.pdf",
}

# Extension preference by accept-attribute fragment.
_ACCEPT_EXT_MAP: list[tuple[str, str]] = [
    ("image/jpeg", "dummy.jpg"),
    ("image/jpg", "dummy.jpg"),
    ("image/png", "dummy.png"),
    (".jpg", "dummy.jpg"),
    (".jpeg", "dummy.jpg"),
    (".png", "dummy.png"),
    ("image/", "dummy.jpg"),
    ("application/pdf", "dummy.pdf"),
    (".pdf", "dummy.pdf"),
    (".docx", "dummy.docx"),
    (".doc", "dummy.docx"),
    ("msword", "dummy.docx"),
    (".csv", "dummy.csv"),
    ("text/csv", "dummy.csv"),
]


def _safe_app_dir(app_name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", app_name.lower()).strip("_")


def discover_test_files(app_name: str) -> list[str]:
    """List test files available to hand to an orchestrator. Looks first in
    the app-specific directory, then falls back to global. Skips JSON files
    (those are config) and the global README. Returns filenames only — the
    orchestrator passes them to upload tools that resolve the full path."""
    root = Path("artifacts/test_files").resolve()
    found: list[str] = []

    app_dir = root / _safe_app_dir(app_name)
    if app_dir.exists():
        for f in sorted(app_dir.iterdir()):
            if f.is_file() and f.suffix.lower() != ".json":
                found.append(f.name)

    gdir = root / "global"
    if gdir.exists():
        for f in sorted(gdir.iterdir()):
            if f.is_file() and f.suffix.lower() != ".json" and f.name != "README.md":
                if f.name not in found:
                    found.append(f.name)

    return found


# Synonyms expand a semantic_hint into alternative words the filename might use.
# E.g. a file named "passport.png" should match hint "id_document".
_HINT_SYNONYMS: dict[str, list[str]] = {
    "id_document": ["passport", "national", "id", "license", "licence", "driver", "permit", "identification"],
    "profile_picture": ["profile", "portrait", "headshot", "photo", "selfie", "avatar"],
    "signature": ["signature", "sign"],
    "proof_of_address": ["address", "proof", "utility", "bill", "statement"],
    "bank_statement": ["bank", "statement", "account"],
    "contract": ["contract", "agreement", "terms"],
}


def _tokenize(text: str) -> set[str]:
    """Split a string into lowercase alphanumeric tokens for overlap scoring."""
    return {t for t in re.split(r"[^a-z0-9]+", text.lower()) if len(t) > 1}


def _score_file(
    filename_stem: str,
    element_name: str,
    semantic_hint: str,
    accept: str,
) -> float:
    """Score how well a filename matches an upload element.

    Weighted so that element-name token matches count 2x synonym matches —
    specific descriptors (e.g. "front"/"back") should beat generic synonyms.
    """
    file_tokens = _tokenize(filename_stem)
    if not file_tokens:
        return 0.0

    element_tokens = _tokenize(element_name)
    synonym_tokens: set[str] = set()
    if semantic_hint:
        synonym_tokens.add(semantic_hint.lower())
        synonym_tokens |= set(_HINT_SYNONYMS.get(semantic_hint.lower(), []))
    # Remove overlap so a token matched via element name isn't double-counted
    synonym_tokens -= element_tokens

    element_overlap = file_tokens & element_tokens
    synonym_overlap = file_tokens & synonym_tokens

    # Weighted score: element-name matches worth 2x synonym matches.
    weighted = len(element_overlap) * 2 + len(synonym_overlap)
    max_possible = len(file_tokens) * 2
    score = weighted / max_possible if max_possible > 0 else 0.0

    # Small boost if accept attribute matches the file extension
    if accept and "." in filename_stem:
        ext = filename_stem.rsplit(".", 1)[-1].lower()
        if ext and (ext in accept.lower() or f".{ext}" in accept.lower()):
            score += 0.1

    return score


def resolve_upload_path(
    element_id: str,
    semantic_hint: str,
    accept: str,
    app_name: str,
    element_name: str = "",
    base_dir: Path | None = None,
) -> str:
    """Return a real test file path for a file_upload element.

    Tier 1: artifacts/test_files/{app}/registry.json[element_id]
    Tier 2: artifacts/test_files/{app}/ — score each file by token overlap
            against element_name + semantic_hint (+ synonyms) + accept
    Tier 3: artifacts/test_files/global/ via _SEMANTIC_DEFAULTS[semantic_hint]
    Tier 4: artifacts/test_files/global/ via accept-attribute match

    If nothing matches, returns the global dummy.pdf path (never empty).
    """
    # Resolve to absolute paths so downstream tools (Chrome DevTools MCP in its
    # own node process) don't interpret paths against a different cwd.
    root = (base_dir or Path("artifacts/test_files")).resolve()
    app_dir = root / _safe_app_dir(app_name)
    global_dir = root / "global"

    # Tier 1 — per-app registry (still supported as an explicit override)
    registry_path = app_dir / "registry.json"
    if registry_path.exists():
        try:
            registry = json.loads(registry_path.read_text())
            if element_id in registry:
                candidate = Path(registry[element_id])
                resolved = candidate if candidate.is_absolute() else root / candidate
                if resolved.exists():
                    return str(resolved)
        except (json.JSONDecodeError, OSError):
            pass

    # Tier 2 — per-app folder, token-overlap scoring
    # Pick the file whose name tokens best overlap with the element's name
    # and semantic_hint (with synonym expansion). Handles cases like
    # "national_id_front.png" matching "Second form of ID front image".
    if app_dir.exists():
        best_file: Path | None = None
        best_score = 0.0
        for f in app_dir.iterdir():
            if not f.is_file() or f.suffix.lower() == ".json":
                continue
            score = _score_file(f.stem, element_name, semantic_hint, accept)
            if score > best_score:
                best_score = score
                best_file = f
        # Require a meaningful match — at least 10% token overlap — to avoid
        # returning the first arbitrary file in the folder.
        if best_file and best_score >= 0.1:
            return str(best_file)

    # Tier 3 — global defaults by semantic_hint
    default_name = _SEMANTIC_DEFAULTS.get(semantic_hint.lower())
    if default_name:
        path = global_dir / default_name
        if path.exists():
            return str(path)

    # Tier 4 — match by accept attribute fragment
    accept_lower = (accept or "").lower()
    for fragment, filename in _ACCEPT_EXT_MAP:
        if fragment in accept_lower:
            path = global_dir / filename
            if path.exists():
                return str(path)

    # Last-ditch fallback — whatever dummy.pdf points at (may not exist)
    fallback = global_dir / "dummy.pdf"
    return str(fallback)
