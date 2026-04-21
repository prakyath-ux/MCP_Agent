# qa/orchestrators/sub_prompts.py — system prompts + JSON schemas for sub-tasks
#
# Each sub-task is one stateless LLM call. Prompts are short and focused —
# the LLM's job is perception/classification, not planning.

# ──────────────────────────────────────────────────────────────────────
# Sub-task 0: detect ALL section tabs on the page
# (one call at the start of the run — Python then loops through them)
# ──────────────────────────────────────────────────────────────────────

DETECT_ALL_SECTIONS_PROMPT = """The page has a multi-section / multi-tab
form. Identify every section TAB that the user must complete in order.

Tabs usually have numbered prefixes like "1. First form of ID",
"2. Second form of ID", "3. Address Proof", or wizard step labels
like "Step 1 — Identity", "Step 2 — Address".

Return tab labels in the order they appear. Preserve case and punctuation
EXACTLY so Python can find the matching uid later.

Do NOT include:
- Top-level page navigation (Contact Info, Documents, etc. — these are
  cross-page nav, not in-page sections)
- Action buttons (Continue, Save, Submit)
- Section headings or descriptive text

If the page only has ONE section (not a multi-tab layout), return a single
entry describing that section's visible title."""

DETECT_ALL_SECTIONS_SCHEMA = {
    "name": "all_sections",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "section_tabs": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Tab labels in order, exactly as they appear",
            },
        },
        "required": ["section_tabs"],
    },
}


# ──────────────────────────────────────────────────────────────────────
# Sub-task 1: find the currently-active section on a gated multi-section page
# ──────────────────────────────────────────────────────────────────────

FIND_ACTIVE_SECTION_PROMPT = """You are looking at a snapshot of a web page
that has a gated multi-section form (e.g., a KYC document verification flow
with tabs like "First form of ID", "Second form of ID", "Address Proof").

Find the CURRENTLY ACTIVE section — the one the user should complete first
(or next). It typically contains a dropdown to choose a document type, and
an upload button that becomes enabled after selection.

Return:
- section_name: short name of the active section (e.g., "First form of ID")
- dropdown_label: the visible label of the dropdown in this section
- options: EVERY option currently visible or listed for that dropdown
- upload_field_label: the visible label next to the upload trigger

If the dropdown is currently closed, only record options you can see in the
snapshot text (don't hallucinate). If no options are visible, return an
empty list — Python will open the dropdown and ask you again."""

FIND_ACTIVE_SECTION_SCHEMA = {
    "name": "active_section",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "section_name": {
                "type": "string",
                "description": "Short name of the active section",
            },
            "dropdown_label": {
                "type": "string",
                "description": "Visible label of the dropdown to populate",
            },
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Every visible dropdown option; [] if closed",
            },
            "upload_field_label": {
                "type": "string",
                "description": "Visible label next to the upload button",
            },
        },
        "required": [
            "section_name",
            "dropdown_label",
            "options",
            "upload_field_label",
        ],
    },
}


# ──────────────────────────────────────────────────────────────────────
# Sub-task 2: extract dropdown options from a snapshot where it's now OPEN
# ──────────────────────────────────────────────────────────────────────

EXTRACT_DROPDOWN_OPTIONS_PROMPT = """The dropdown in the snapshot below is
currently OPEN and its options are visible in the accessibility tree.

Return EVERY option text exactly as it appears. Preserve case and
punctuation. Do NOT add options that aren't present. Do NOT include
placeholder text like "Select an option" or "Choose one"."""

EXTRACT_DROPDOWN_OPTIONS_SCHEMA = {
    "name": "dropdown_options",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "options": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
        "required": ["options"],
    },
}


# ──────────────────────────────────────────────────────────────────────
# Sub-task 3: classify the page state right after a file was ATTACHED
# (but before OCR runs). Many apps require a separate click — "Upload",
# "Verify", "Continue" — to submit the file and trigger OCR.
# ──────────────────────────────────────────────────────────────────────

CLASSIFY_POST_ATTACH_STATE_PROMPT = """A file has just been attached to an
input[type=file] on a document-upload section (KYC / proof-of-identity).
Three things are possible now:

1. OCR is ALREADY running — you'll see "Verifying", "Processing",
   "Analyzing", a spinner, or a loading indicator near the upload.
2. OCR has ALREADY COMPLETED — new auto-filled fields (first name, DOB,
   document number, etc.) are already visible on the page.
3. Nothing is happening — the file is attached but the user must click
   a button (often labelled "Upload", "Verify", "Submit document",
   "Continue", or similar) to submit it and start OCR.

Return which state the page is in. If state 3, identify the most likely
button to click — it should confirm THIS section without navigating to
the next page. Treat buttons like "Save & Continue", "Save & Exit",
"Submit Application", "Register", "Next Page", "Log In" as UNSAFE —
they navigate away from the current section.

Safe button labels typically include: Upload, Verify, Submit Document,
Continue (within-section only), Confirm, Start Verification, Process,
Analyze, Proceed, Start OCR.

If no trigger button exists, return button_label="". Do NOT invent a
button that isn't clearly visible in the snapshot."""

CLASSIFY_POST_ATTACH_STATE_SCHEMA = {
    "name": "post_attach_state",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "state": {
                "type": "string",
                "enum": ["ocr_running", "ocr_completed", "needs_button_click"],
            },
            "button_label": {
                "type": "string",
                "description": (
                    "Visible label of the safe button to click. "
                    "Empty string if state is not 'needs_button_click' "
                    "or if no safe button is visible."
                ),
            },
            "button_is_safe": {
                "type": "boolean",
                "description": (
                    "True if the button confirms the current section "
                    "only (not page-level Submit/Save & Continue)."
                ),
            },
            "reasoning": {
                "type": "string",
                "description": "One short sentence explaining the choice.",
            },
        },
        "required": ["state", "button_label", "button_is_safe", "reasoning"],
    },
}


# ──────────────────────────────────────────────────────────────────────
# Sub-task 4: extract fields that appeared after OCR/upload completed
# ──────────────────────────────────────────────────────────────────────

EXTRACT_POST_OCR_FIELDS_PROMPT = """The snapshot below is from AFTER an
upload was verified by OCR. New form fields were auto-rendered with
pre-populated values (first name, last name, DOB, document number, etc.).

Extract EVERY new form field that appeared. For each field return:
- name: the visible label
- type: one of "text_input" | "email" | "phone" | "date" | "dropdown" | "checkbox" | "radio" | "file_upload" | "button"
- required: true if marked required (asterisk, "required" text)
- value_entered: current value if pre-populated, else ""
- behavior: any observed behavior like "read_only", "masked", "auto_filled"

Only include fields visible in THIS snapshot. Do not include fields that
were already on the page before the upload. Do not invent fields you
can't see."""

EXTRACT_POST_OCR_FIELDS_SCHEMA = {
    "name": "post_ocr_fields",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "fields": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "name": {"type": "string"},
                        "type": {
                            "type": "string",
                            "enum": [
                                "text_input", "email", "phone", "date",
                                "dropdown", "checkbox", "radio",
                                "file_upload", "button",
                            ],
                        },
                        "required": {"type": "boolean"},
                        "value_entered": {"type": "string"},
                        "behavior": {"type": "string"},
                    },
                    "required": [
                        "name", "type", "required", "value_entered", "behavior",
                    ],
                },
            },
        },
        "required": ["fields"],
    },
}


# ──────────────────────────────────────────────────────────────────────
# Sub-task 5: Classify a single test-case outcome from pre+post snapshots.
# Used by ExecuteOrchestrator — Python fills the field, LLM observes the
# result, returns PASS/FAIL with evidence. Python does NOT rely on this
# for navigation or state mutation — purely an observer role.
# ──────────────────────────────────────────────────────────────────────

CLASSIFY_TEST_RESULT_PROMPT = """You are observing the outcome of a single
test case on a web form field. Python has already filled the field with
the test value; your job is ONLY to observe what happened and classify.

Given:
  - field_name: the label of the tested field
  - approach: how the test was performed (fill_check / tap_verify / verify_only)
  - test_value: what was entered (may be empty for verify_only)
  - expected_result: what SHOULD happen
  - pre_snapshot: state before the test was performed
  - post_snapshot: state after the test was performed

Classification rubric — apply in order:

1. Expected behavior is an ERROR (expected_result mentions
   "error", "required", "validation", "format", "invalid"):
   - Error/validation text visible in post_snapshot → PASS
   - No error visible → FAIL (the app failed to validate)

2. Expected behavior is ACCEPTANCE (expected_result mentions
   "accepts", "no_error", "valid", "saved", "filled"):
   - Value present in field in post_snapshot AND no error visible → PASS
     (many apps defer validation until form submit — absence of inline
     error when filling a valid value IS the correct observable signal)
   - Value missing or reverted to previous → FAIL
   - Error visible → FAIL (app incorrectly rejected a valid value)

3. Expected behavior is NO-CHANGE (expected_result mentions
   "readonly", "no_change", "unchanged", "disabled"):
   - Value in post_snapshot matches pre_snapshot → PASS
   - Value changed → FAIL

4. Only use BLOCKED when the field, dropdown menu, or required UI
   element is genuinely not visible in either snapshot (page in
   unexpected state, modal blocking view, element hidden). BLOCKED
   is NOT for "no inline feedback visible" — that's case 2 above.

Provide:
  - observed: one-sentence description of what actually happened
  - error_text: the exact error/validation text if one appeared, else empty string
  - confidence: 0.0-1.0 — how confident you are in the classification

Do not guess about behavior you cannot see in the snapshots. Do not
suggest fixes. Only report what the snapshots literally show."""

CLASSIFY_TEST_RESULT_SCHEMA = {
    "name": "classify_test_result",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "status": {
                "type": "string",
                "enum": ["pass", "fail", "blocked"],
            },
            "observed": {
                "type": "string",
                "description": "One sentence on what happened",
            },
            "error_text": {
                "type": "string",
                "description": "Exact error/validation text if shown, else empty",
            },
            "confidence": {
                "type": "number",
                "description": "0.0 - 1.0 confidence in the classification",
            },
        },
        "required": ["status", "observed", "error_text", "confidence"],
    },
}


# ──────────────────────────────────────────────────────────────────────
# Sub-task 6: Chain-of-Verification — is a list of claimed items actually
# supported by the snapshot? Used by qa.orchestrators.verify for Tier 2
# semantic rescue when deterministic string-match is too strict.
# ──────────────────────────────────────────────────────────────────────

VERIFY_CLAIM_PROMPT = """You are verifying whether a list of claims is
supported by a source snapshot. For each claim item, decide:

- Supported (literally or semantically present in the snapshot) → verified_items
- NOT present (hallucinated / invented) → unsupported_items

Be STRICT. If you aren't sure an item is in the snapshot, put it in
unsupported_items. Return items verbatim as given — do not rewrite."""

VERIFY_CLAIM_SCHEMA = {
    "name": "verify_claim",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "verified_items": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Items from the claim that ARE supported by the snapshot, verbatim",
            },
            "unsupported_items": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Items from the claim that are NOT in the snapshot (hallucinated)",
            },
            "reasoning": {
                "type": "string",
                "description": "One short sentence on how the verification was decided",
            },
        },
        "required": ["verified_items", "unsupported_items", "reasoning"],
    },
}
