# qa/models/app_fingerprint.py — Wall 2.0: App Selector DNA
#
# A one-shot-learned-per-app "fingerprint" of the DOM shape patterns the
# app uses. Learned via qa/orchestrators/app_probe.py, stored at
# artifacts/knowledge/web/<app>.meta.json, and consumed by form_extract
# as strategy-0 before falling back to the existing hand-written
# strategies (tecu_label_for, mui_formcontrol, p_tag, generic_text_match).
#
# The key insight: don't detect "frameworks" — Tailwind is CSS only, two
# Next.js apps can use different component libs. Instead, learn each
# app's selector DNA once and reuse. If the app's component library
# changes, re-run the probe (via --reprobe) and the fingerprint heals.

from datetime import datetime

from pydantic import BaseModel, Field


class DropdownFingerprint(BaseModel):
    """CSS selectors + binding strategy for this app's custom dropdowns.

    An empty trigger_selector signals "probe did not find a consistent
    dropdown pattern" — form_extract will fall back to the hand-written
    strategies. Populate only when the probe has high confidence."""
    trigger_selector: str = ""       # CSS for the clickable trigger element
    menu_selector: str = ""          # CSS for the open-popup container
    option_selector: str = ""        # CSS for individual options in the menu
    # How the trigger is associated with its visible label. Formats:
    #   "label_for"                        — HTML5 <label for="X">
    #   "ancestor:<sel> child:<sel>"       — label is inside a shared ancestor
    #   "sibling_prev"                     — label is the immediate previous sibling
    #   ""                                 — unknown, fall back to text match
    label_binding: str = ""
    notes: str = ""


class TextInputFingerprint(BaseModel):
    """Rules for capturing plain text inputs on this app."""
    # How the visible label is bound to the input. Same formats as
    # DropdownFingerprint.label_binding.
    label_binding: str = ""
    # Should placeholder text be treated as a label when nothing else
    # is available? True for apps that use placeholders as primary
    # labels (rare); False for apps that use real labels + placeholders
    # as example text (Oxd, MUI). Defaulting False avoids extracting
    # noise like "Yyyy-Dd-Mm" as a field name.
    use_placeholder_as_label: bool = True
    # When False, the enumerate will include inputs that have no id /
    # name / placeholder IF we can resolve a label for them via the
    # label_binding. Default True preserves current form_extract
    # behaviour for unprobed apps.
    require_id_or_name_or_placeholder: bool = True


class DatePickerFingerprint(BaseModel):
    """How date-picker widgets are distinguishable from plain text inputs."""
    # CSS for the date-input element itself (often a text input with a
    # specific class / role / sibling icon).
    input_selector: str = ""
    # Pattern that flags a plain input as a date. One of:
    #   "input_type_date"                  — <input type=date>
    #   "sibling_icon_button"              — adjacent calendar icon button
    #   "placeholder_format"               — placeholder matches a date format
    #   "class_match"                      — specific class on container
    detection_pattern: str = ""
    notes: str = ""


class RadioFingerprint(BaseModel):
    """How this app structures radio groups."""
    # CSS for the wrapper container holding all radios in a group.
    wrapper_selector: str = ""
    # CSS (relative to wrapper) for each radio's visible label text.
    option_label_selector: str = ""
    notes: str = ""


class FileUploadFingerprint(BaseModel):
    """How this app exposes file uploads."""
    # CSS for the user-visible trigger (often a button, not the hidden input).
    trigger_selector: str = ""
    # Whether clicking the trigger reveals a hidden <input type=file>
    # (TECU custom-button pattern) or the input is directly in the DOM
    # (native / Oxd simple upload).
    requires_click_to_expose: bool = False
    notes: str = ""


class AppFingerprint(BaseModel):
    """Complete selector DNA for a web application.

    Produced once by the probe orchestrator, persisted to
    artifacts/knowledge/web/<app>.meta.json, consumed by form_extract
    as strategy 0 before the hand-written strategies run.

    Confidence < 0.5 means the probe was uncertain; consumers should
    treat the fingerprint as advisory and still run the hand-written
    fallbacks. Confidence >= 0.7 means the fingerprint should win.
    """
    app_name: str
    probed_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    # Human-readable hint for debugging. "oxd-vue", "mui-react", "tecu-html5",
    # "bootstrap", "unknown". Not used in code paths — purely informational.
    framework_signature: str = ""
    # Per-element-type fingerprints. Any may be empty (probe didn't find
    # that pattern on the probed page) — consumers check emptiness and
    # skip / fall back accordingly.
    dropdown: DropdownFingerprint = Field(default_factory=DropdownFingerprint)
    text_input: TextInputFingerprint = Field(default_factory=TextInputFingerprint)
    date_picker: DatePickerFingerprint = Field(default_factory=DatePickerFingerprint)
    radio: RadioFingerprint = Field(default_factory=RadioFingerprint)
    file_upload: FileUploadFingerprint = Field(default_factory=FileUploadFingerprint)
    confidence: float = 0.0
    # Free-form list of signals the probe used to derive the fingerprint —
    # helpful for debugging a bad probe run ("saw .oxd-select-text on 4
    # fields but no MUI roots → picked oxd").
    evidence_seen: list[str] = Field(default_factory=list)
