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


# ──────────────────────────────────────────────────────────────────────
# Sub-task: classify a "transition" — did clicking a nav button take us
# to a genuinely new page, or did the same page just re-render with an
# error / expansion / revealed section?
# Used by wizard mode after wait_for_page_transition fires.
# ──────────────────────────────────────────────────────────────────────

CLASSIFY_PAGE_TRANSITION_PROMPT = """You will receive two snapshots of
a web page taken before and after the wizard clicked a 'Save & Continue'
or 'Next' / 'Verify' button.

Your job: decide which of these happened.

  • NEW_PAGE — the user genuinely advanced to a new page or wizard step.
    Look for: different page heading text, different URL, completely
    different field set, a different section header.

  • SAME_PAGE_WITH_ERROR — the click was rejected. The app is still on
    the original page but is now showing a validation message, error
    banner, "user already exists", "field is required", a red toast,
    inline field errors, etc. Most fields from the BEFORE snapshot
    are still present in the AFTER snapshot.

  • SAME_PAGE_WITH_EXPANSION — the click revealed a sub-section on the
    SAME page (e.g. expanded an accordion, revealed an OTP block,
    showed a previously-hidden upload row). All BEFORE fields still
    visible AND new fields appeared in the same screen context.

If you're unsure, lean toward SAME_PAGE_WITH_ERROR — wrongly accepting
a fake transition pollutes the KB; wrongly rejecting a real one only
costs another extract on retry.

Provide a one-sentence reason and, if relevant, the error_text you saw
verbatim from the snapshot."""

CLASSIFY_PAGE_TRANSITION_SCHEMA = {
    "name": "page_transition",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "verdict": {
                "type": "string",
                "enum": [
                    "NEW_PAGE",
                    "SAME_PAGE_WITH_ERROR",
                    "SAME_PAGE_WITH_EXPANSION",
                ],
            },
            "reasoning": {
                "type": "string",
                "description": "One short sentence explaining the verdict",
            },
            "error_text": {
                "type": "string",
                "description": "Verbatim error/validation text from the after-snapshot, or '' if none",
            },
        },
        "required": ["verdict", "reasoning", "error_text"],
    },
}


# ──────────────────────────────────────────────────────────────────────
# Sub-task: tag each L0 field as NEW (specific to this screen) or
# CARRYOVER (already captured on a prior screen / part of the persistent
# header / not testable here). Lets the wizard write a clean per-screen
# KB instead of dumping the whole DOM into every page entry.
# ──────────────────────────────────────────────────────────────────────

TAG_NEW_VS_CARRYOVER_PROMPT = """You will receive:
  1. A list of field names captured on the CURRENT page.
  2. A list of field names already captured on PRIOR pages of the same
     wizard / form.
  3. The current page's snapshot (a11y tree).

Tag each current field as NEW or CARRYOVER:

  • NEW — the field is specific to this page, the user is meant to
    interact with it here for the first time. Even if a similarly-named
    field exists on a prior page, mark NEW if THIS instance is in a
    different section / serves a different purpose / is freshly
    rendered for this step.

  • CARRYOVER — the field is just the same one as before, persisting in
    the DOM across pages. Common patterns: a sticky header summarising
    the user's name, or fields that the framework re-renders identically
    on every wizard step. The user has already filled it. Do not test
    it again here.

Heuristic: if the field appears on prior pages AND it's in a header /
summary section of the current page (vs. inside the current step's
main form area), it's almost always CARRYOVER. New fields specific to
this step are usually NEW even if they share a name with something
historical.

Return one entry per field in the input list, preserving order."""

TAG_NEW_VS_CARRYOVER_SCHEMA = {
    "name": "field_tags",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "tags": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "field_name": {"type": "string"},
                        "tag": {
                            "type": "string",
                            "enum": ["NEW", "CARRYOVER"],
                        },
                        "reason": {"type": "string"},
                    },
                    "required": ["field_name", "tag", "reason"],
                },
            },
        },
        "required": ["tags"],
    },
}


# ──────────────────────────────────────────────────────────────────────
# Sub-task: pick a strategy for the current page (Phase 2 / Strategist).
# Given a snapshot, the LLM chooses which registered strategy applies.
# Strategies are app-agnostic recipes that compose primitives — the
# strategy name maps to a Python handler function in
# qa.orchestrators.strategies.
# ──────────────────────────────────────────────────────────────────────

PICK_STRATEGY_PROMPT = """You are looking at a web page snapshot. Pick
which strategy the autonomous testing agent should run on this page.

Available strategies:

  • wizard_step
      A flat form: a set of input fields (text/email/phone/dropdown),
      possibly file uploads, with one navigation button (Save & Continue
      / Next / Submit) at the bottom. Wizard fills every field from
      defaults, optionally handles an inline OTP block if present, and
      clicks the navigation button. Use this for normal personal-
      information / additional-details / preferences pages.

  • gated_step
      A multi-section page where each section has its own
      dropdown → file upload → OCR → auto-fill cycle. Look for: section
      tabs ("1. First form of ID", "2. Second form of ID", ...), each
      tab containing a "Choose document type" dropdown and an upload
      slot. KYC / document verification pages match this. Wizard fill
      will NOT work here — each section needs its own complete cycle.

  • terminal
      The form is complete. Look for: a "Thank you", "Application
      submitted", "Welcome <name>", confirmation number, success page,
      or a final review-only screen with no editable inputs. Stop the
      run cleanly.

  • blocked
      The page exists but the agent can't proceed: a login screen
      requesting credentials we don't have, a CAPTCHA, a 2FA prompt,
      a "Service unavailable" error, an unexpected redirect to
      something other than the expected app URL. Stop and report.

  • unknown
      The page doesn't fit any of the above patterns clearly. Examples:
      a heavily custom widget set (drag-drop UI, canvas-based form,
      pure JS picker with no <input>), a shadow-DOM or iframe-bound
      form, or anything where the strategy isn't obvious. Stop with a
      clear note so a human can extend the strategy library.

Decision rules:
- If you see numbered section tabs each with their own doc-type
  dropdown + upload, pick gated_step.
- If you see a flat form with text/dropdown fields and a single
  Save & Continue button, pick wizard_step. An OTP block in the
  same view is fine — wizard handles it.
- If the page is clearly post-completion (no editable inputs, success
  message), pick terminal.
- If it's a login / CAPTCHA / error page, pick blocked.
- If you can't tell, pick unknown — never guess.

Provide one short reasoning sentence and confidence ('high' if obvious,
'low' if you'd have flipped a coin)."""

PICK_STRATEGY_SCHEMA = {
    "name": "pick_strategy",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "strategy": {
                "type": "string",
                "enum": [
                    "wizard_step",
                    "gated_step",
                    "terminal",
                    "blocked",
                    "unknown",
                ],
            },
            "confidence": {
                "type": "string",
                "enum": ["high", "medium", "low"],
            },
            "reasoning": {
                "type": "string",
                "description": "One short sentence explaining the choice",
            },
        },
        "required": ["strategy", "confidence", "reasoning"],
    },
}


# ──────────────────────────────────────────────────────────────────────
# Sub-task: identify reveal-on-click buttons that the wizard should
# press BEFORE the final Save & Continue. Catches "+ Add Another",
# "Show Advanced", "Add Beneficiary", expand-section toggles, etc.
# Does NOT include Save / Submit / Cancel / navigation — those advance
# the form, not reveal new fields.
# ──────────────────────────────────────────────────────────────────────

FIND_REVEAL_BUTTONS_PROMPT = """The wizard has filled the form fields
on this page from defaults. Before clicking the final Save & Continue
/ Submit button, identify any in-page buttons whose purpose is to
REVEAL or ADD MORE form fields (not navigate / submit).

A reveal button has ALL of these properties:
  • Visible button role (uid + role 'button' / 'link' in the snapshot)
  • Label clearly indicates adding / showing / expanding more form
    content (the literal action — not just a noun)
  • Clicking it would surface NEW editable fields, not change state
    of an existing field

Examples of valid reveal-on-click buttons:
  • "+ Add Another"
  • "Add Beneficiary"
  • "Show Advanced Options"
  • "More Details"
  • "Add Income Source"
  • "Expand"

NEVER include any of the following — even if they're styled as
buttons in the DOM:

  • Save & Continue / Save & Exit / Save / Submit / Next / Previous /
    Back / Continue / Cancel / Discard / Clear / Reset — these
    advance / cancel the form
  • Delete / Remove / Trash — destructive
  • Verify OTP / Confirm OTP / Resend OTP — OTP flow, not reveal
  • Re-upload / Replace / Change — file replacement
  • Section navigation tabs (numbered: "1. ", "2. ", "Step 3:")
  • The DISPLAY LABEL of an already-selected dropdown
    (e.g. "100 - TECU - MARABELLA BRANCH" appears as a button after
    selection — that's the dropdown's chosen-value display, NOT a
    reveal button)
  • An option text from inside a now-closed dropdown popup
  • Filename / item-summary buttons that show what was uploaded
    (e.g. "passport.png", "delete attachment")
  • Static text rendered as a button-like span/div with no actual
    action handler

The safest default is an empty array. Only return a button if you're
certain it would add new fillable fields. If you're guessing,
return empty.

Return the EXACT visible label so Python can find the uid."""

FIND_REVEAL_BUTTONS_SCHEMA = {
    "name": "reveal_buttons",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "buttons": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "label": {"type": "string"},
                        "reasoning": {"type": "string"},
                    },
                    "required": ["label", "reasoning"],
                },
                "description": "Reveal-on-click buttons to press before the final nav, in priority order",
            },
        },
        "required": ["buttons"],
    },
}


# ──────────────────────────────────────────────────────────────────────
# Sub-task: when a fill fails or the form rejects a value, suggest an
# alternate value to try. The retry helper invokes this with the field's
# context (name, type, validation_rules), the value we tried, what the
# field now shows, and any error_text the page surfaces.
# ──────────────────────────────────────────────────────────────────────

SUGGEST_ALTERNATE_VALUE_PROMPT = """A form field rejected the value
the wizard tried. Suggest an alternate value the field is most likely
to accept based on its name, type, the value attempted, what's
currently in the field, and the error message (if any).

Constraints:
  • Output a SINGLE alternate value as a string.
  • If the error suggests the field needs a different format (e.g.
    'phone must be 10 digits' but we sent 7), produce a value matching
    that format.
  • If the error suggests duplicate / already-used, alter the value
    enough to be unique (add/change a few characters).
  • If you can't tell what would work, return an empty string ''.
  • Do NOT include explanation text in the value itself — value field
    only contains the literal text to type.

Reasoning field: 1 short sentence on why this alternate should work."""

SUGGEST_ALTERNATE_VALUE_SCHEMA = {
    "name": "alternate_value",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "value": {
                "type": "string",
                "description": "Alternate value to try, or '' if no plausible alternate",
            },
            "reasoning": {"type": "string"},
        },
        "required": ["value", "reasoning"],
    },
}


# ──────────────────────────────────────────────────────────────────────
# Sub-task: find unanswered decision-button groups on the page.
# A decision group is 2+ adjacent buttons that present mutually-
# exclusive options the user must pick one of (Yes/No, Accept/Decline,
# Single/Married/Divorced, "Yes - I am a US citizen" / "No"). The
# agent answers each by trying options in order and keeping the one
# that doesn't expand the form (collapse-preferring strategy).
# ──────────────────────────────────────────────────────────────────────

FIND_DECISION_GROUPS_PROMPT = """The wizard has filled the page's
input fields. Identify any UNANSWERED decision-button groups the user
still needs to resolve before the form can submit.

A decision-button group has ALL of these properties:
  • 2 or more adjacent buttons clustered together (same line, same
    fieldset, or directly under the same prompt/question)
  • The buttons present mutually-exclusive choices — picking one
    excludes the others (Yes vs No, Accept vs Decline, Single vs
    Married, Domestic vs International)
  • Currently NO option appears selected — neither button shows a
    selected/active style (no checkmark, no highlighted background,
    no "Yes - already chosen" text)
  • Clicking one is required to advance the form

NEVER return:
  • Save & Continue / Save & Exit / Submit / Next / Back / Cancel —
    these are nav buttons, not decision groups
  • A group where one option clearly already shows as selected
  • Single buttons not part of a 2+ option choice
  • Multi-step tab navigation ("1. First form of ID", "2. Second...")
  • Action buttons inside a section (Verify OTP, Re-upload)
  • A list of dropdown options inside an open popup
  • Already-completed acknowledgement buttons / disclosed checkboxes

Return an ordered list of groups, each with the EXACT button labels
so Python can find their uids. If you're unsure whether something is
a decision group or a layout artefact, leave it out — empty array
is the safe default."""

FIND_DECISION_GROUPS_SCHEMA = {
    "name": "decision_groups",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "groups": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "question": {
                            "type": "string",
                            "description": "Prompt/question text above the buttons, or '' if none",
                        },
                        "options": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Exact button labels in display order",
                        },
                    },
                    "required": ["question", "options"],
                },
            },
        },
        "required": ["groups"],
    },
}
