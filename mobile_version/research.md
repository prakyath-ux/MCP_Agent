# Mobile App Testing Agent — Research

## Why mobile-mcp Over the Others

| Criteria | mobile-mcp | appium-mcp | Maestro MCP | android-mcp-server |
|----------|-----------|------------|-------------|-------------------|
| **Setup** | `npx` + ADB only | Appium server + JDK + Android SDK config | Maestro CLI install | Python + ADB |
| **Dependencies** | 1 (ADB) | 4 (Appium, JDK, Android SDK, capabilities.json) | 1 (Maestro) | 2 (Python, ADB) |
| **Community** | **4,100 stars** | 255 stars | Embedded in Maestro CLI | 709 stars |
| **Tools** | 21 (clean, focused) | 56+ (bloated, overwhelming for LLM) | 14 (generates YAML, not direct control) | 5 (too basic) |
| **Appium needed?** | **No** | Yes (separate process) | No | No |
| **LLM-friendly?** | **Yes** — 15 tools, small schema | No — 56 tools = 3K+ tokens of schemas | Partial — writes YAML, not direct interaction | Partial — raw ADB commands |
| **SDK compatible?** | **Yes** — MCPServerStdio | Yes | Yes | Yes |
| **Approach** | Direct tap/swipe/type | Session-based, element locators | YAML flow generation | Raw ADB commands |

### Key Reasons for Choosing mobile-mcp

1. **15 tools vs 56** — LLMs choke on too many tools. Proven in web agent testing: free models (Groq Llama, OpenRouter Gemini) failed with 26 Chrome DevTools tools. 56 tools from appium-mcp would be worse. mobile-mcp's 15 is manageable.

2. **No Appium dependency** — Appium requires a separate server process, JDK, capabilities configuration, and session management. mobile-mcp talks directly to ADB. Fewer moving parts = fewer failure points.

3. **Most proven** — 4,100 GitHub stars vs 255. Larger community means more bugs found/fixed and better documentation.

4. **Maps to our web agent pattern** — tool mapping is nearly 1:1:

   | Web (Chrome DevTools MCP) | Mobile (mobile-mcp) |
   |--------------------------|-------------------|
   | `click(uid)` | `tap(x, y)` |
   | `fill(uid, value)` | `type_text(text)` |
   | `take_snapshot` | `list_ui_elements` |
   | `take_screenshot` | `take_screenshot` |
   | `navigate_page(url)` | `launch_app(package)` |
   | `evaluate_script(js)` | No equivalent (native apps have no JS) |

5. **RAM-friendly** — No Appium server eating resources. Important on 16GB Mac running emulator + agent simultaneously.

---

## mobile-mcp Overview

- **Repo:** github.com/mobile-next/mobile-mcp
- **npm:** `@mobilenext/mobile-mcp`
- **Transport:** stdio (same as Chrome DevTools MCP)
- **Platforms:** Android (full), iOS (partial/beta)
- **Maturity:** Beta, pre-1.0 — version pin mandatory

### Available Tools (21 standard + 3 fleet)

**Device Management (4)**

| Tool | What it does | Web equivalent |
|------|-------------|----------------|
| `mobile_list_available_devices` | List all connected devices, simulators, emulators | N/A |
| `mobile_get_screen_size` | Get screen size in pixels | N/A |
| `mobile_get_orientation` | Get current orientation (portrait/landscape) | N/A |
| `mobile_set_orientation` | Change screen orientation | N/A |

**App Management (5)**

| Tool | What it does | Web equivalent |
|------|-------------|----------------|
| `mobile_launch_app` | Open app by package name | `navigate_page` |
| `mobile_terminate_app` | Stop and close an app | N/A |
| `mobile_install_app` | Install APK/IPA on device | N/A |
| `mobile_uninstall_app` | Remove app from device | N/A |
| `mobile_list_apps` | List all installed apps | N/A |

**Screen Inspection (3)**

| Tool | What it does | Web equivalent |
|------|-------------|----------------|
| `mobile_take_screenshot` | Capture current screen (returns image) | `take_screenshot` |
| `mobile_save_screenshot` | Save screenshot to disk | N/A |
| `mobile_list_elements_on_screen` | Get accessibility tree with coordinates, text, labels | `take_snapshot` |

**Touch Interaction (4)**

| Tool | What it does | Web equivalent |
|------|-------------|----------------|
| `mobile_click_on_screen_at_coordinates` | Tap at (x, y) pixels | `click` |
| `mobile_double_tap_on_screen` | Double tap at (x, y) | N/A |
| `mobile_long_press_on_screen_at_coordinates` | Long press at (x, y) | N/A |
| `mobile_swipe_on_screen` | Swipe in a direction (scroll, pull to refresh) | N/A |

**Input & Navigation (3)**

| Tool | What it does | Web equivalent |
|------|-------------|----------------|
| `mobile_type_keys` | Type text into focused element | `fill` |
| `mobile_press_button` | Press hardware button (HOME, BACK, ENTER, VOLUME) | N/A |
| `mobile_open_url` | Open URL in device browser | `navigate_page` |

**Screen Recording (2)**

| Tool | What it does | Web equivalent |
|------|-------------|----------------|
| `mobile_start_screen_recording` | Record screen in background | N/A |
| `mobile_stop_screen_recording` | Stop recording, returns file path | N/A |

**Fleet Management (3 — only with MOBILEFLEET_ENABLE=1)**

| Tool | What it does |
|------|-------------|
| `mobile_list_fleet_devices` | List remote fleet devices |
| `mobile_allocate_fleet_device` | Reserve a remote device |
| `mobile_release_fleet_device` | Release a remote device |

> **Note:** Screen recording has no web equivalent — valuable for test run evidence and demo recordings.

### How It Finds Elements

mobile-mcp uses a **hybrid approach**:
1. **Accessibility tree first** — structured data with element types, labels, bounds
2. **Screenshot fallback** — coordinate-based detection when a11y data unavailable

This is similar to Chrome DevTools MCP using accessibility snapshots. The agent reads the UI tree, identifies elements, and interacts via coordinates or element references.

---

## Key Differences: Web vs Mobile Testing

| Aspect | Web (current) | Mobile (new) |
|--------|--------------|-------------|
| **Element identification** | UID from snapshot, CSS selectors, XPaths | Coordinates (x, y), accessibility labels |
| **Text input** | `fill(uid, value)` sets value directly | `tap` on field → `type_text` (simulates keyboard) |
| **Validation checking** | `evaluate_script` reads DOM for error elements | `list_ui_elements` + look for error text in a11y tree |
| **Navigation** | URL-based (`navigate_page`) | App-based (`launch_app`, `press_button(back)`) |
| **Page state** | DOM is inspectable via JS | Only accessible via a11y tree or screenshots |
| **Dropdowns** | Custom modals, problematic | Native Android spinners, usually simpler |
| **File uploads** | Hidden input[type=file], complex | System intents, different approach |
| **Scrolling** | Page scrolls automatically | Must use `swipe` to reveal off-screen elements |
| **Multiple pages** | URL changes | Screen/Activity changes, no URL |

---

## Infrastructure Requirements

### Hardware
- Mac with 16GB RAM (confirmed available)
- 102GB free disk (confirmed available)
- USB cable for connecting Android phone

### Software
- **ADB** — `brew install android-platform-tools`
- **mobile-mcp** — `npx -y @mobilenext/mobile-mcp@latest`
- **Android phone** with USB Debugging enabled (provided by lead)

### Phone Setup
1. Settings → About Phone → tap "Build Number" 7 times (enables Developer Options)
2. Settings → Developer Options → enable "USB Debugging"
3. Connect via USB
4. Run `adb devices` — device should appear
5. Install the bank app APK: `adb install path/to/app.apk`

### No Android Studio Needed
Using a real phone eliminates the need for Android Studio and the emulator. Saves ~3GB disk and ~4GB RAM.

---

## Integration with Existing Agent

### What We Reuse (from version_2)
- **orchestrator.py** — turn loop, budget tracking, phase switching (needs minor adaptation)
- **compactor.py** — rolling summary, history compaction (reuse as-is)
- **config.py** — constants, pricing, turn limits (add mobile-specific settings)
- **run.py** — CLI entrypoint (add mobile mode)
- **Cost tracking** — per-turn token logging (reuse as-is)
- **Loop detection** — Fix A circuit breaker (reuse as-is)
- **Phase 2a/2b architecture** — plan test cases → execute (reuse concept)

### What We Build New
- **mobile_prompts.py** — system prompt for mobile context (element coordinates, swipe, a11y tree)
- **mobile_orchestrator.py** — MCP server swap (mobile-mcp instead of Chrome DevTools)
- **Element mapping** — translate a11y tree to testable elements (different from DOM/XPath)
- **Scroll handling** — logic to swipe and discover off-screen elements
- **Knowledge JSON format** — adapted for mobile elements (coordinates, bounds, a11y labels)

### What Changes
- No `evaluate_script` — native apps have no JavaScript engine
- No CSS selectors — elements identified by a11y labels, resource-id, or coordinates
- No `fill(uid, value)` — must `tap` field first, then `type_text`
- Snapshots return a11y tree format, not HTML DOM
- Error detection via visual a11y tree reading, not DOM query

---

## Verified: mobile_list_elements_on_screen Output (2026-03-24)

Tested on real device (SM-M356B, Android 16) with bank app (iTeller screen). Output is **clean flat JSON** — much better than raw ADB XML dump.

### Sample element from output:
```json
{
  "type": "android.widget.EditText",
  "text": "Enter Details",
  "label": "Enter Details",
  "coordinates": {"x": 141, "y": 1422, "width": 885, "height": 136},
  "focused": true
}
```

### What each field means:
| Field | Description | Web equivalent |
|-------|-------------|----------------|
| `type` | Android widget class (EditText, TextView, ViewGroup) | HTML tag (`<input>`, `<button>`, `<div>`) |
| `text` | Visible text on the element | `textContent` |
| `label` | Accessibility label (screen reader text) | `aria-label` |
| `identifier` | Developer-assigned ID (when available) | `id` / `resource-id` |
| `coordinates` | Position: x, y, width, height in pixels | `bounds` in CSS |
| `focused` | Whether element has focus (only on active elements) | `:focus` pseudo-class |

### How agent calculates tap point:
```
center_x = coordinates.x + coordinates.width / 2
center_y = coordinates.y + coordinates.height / 2

Example: "Enter Details" field
  center_x = 141 + 885/2 = 583
  center_y = 1422 + 136/2 = 1490
  → mobile_click_on_screen_at_coordinates(x=583, y=1490)
```

### Bank App (iTeller) Elements Found:

| Element | Type | Text/Label | Tap Center |
|---------|------|-----------|------------|
| Back button | ViewGroup | identifier: "backButton" | (65, 179) |
| Title | TextView | "iTeller" | — |
| Select Transaction Type | ViewGroup | label: "Select Transaction Type" | (573, 1189) |
| Select Search Criteria | ViewGroup | label: "Select Search Criteria" | (573, 1336) |
| Enter Details | **EditText** | text: "Enter Details" | (583, 1490) |
| Date of Birth | ViewGroup | label: "Date of Birth" | (583, 1646) |
| Find Member | ViewGroup | label: "Find Member" | (540, 1978) |
| DASHBOARD | Nav tab | label: "DASHBOARD" | (108, 2229) |
| iTELLER | Nav tab | label: "iTELLER" | (324, 2229) |
| iBRANCH | Nav tab | label: "iBRANCH" | (540, 2229) |
| LOAN | Nav tab | label: "LOAN" | (756, 2229) |
| MORE | Nav tab | label: "MORE" | (972, 2229) |

### Key Findings:
1. **Output is flat JSON list** — not nested XML like raw ADB. LLM-friendly.
2. **Coordinates are pre-calculated** — x, y, width, height. No bounds parsing needed.
3. **Labels are readable** — "Select Transaction Type", "Find Member", "Date of Birth".
4. **`identifier` available on some elements** — "backButton", "headerImage". Not on all.
5. **`type` distinguishes inputs** — `EditText` = text field, `ViewGroup` = container/button, `TextView` = label.
6. **Screen size is 1080x2340** — coordinates are absolute pixels on this device.

### Comparison: Raw ADB vs mobile-mcp

| Aspect | Raw ADB (`uiautomator dump`) | mobile-mcp (`list_elements_on_screen`) |
|--------|------------------------------|---------------------------------------|
| Format | Nested XML, 45KB | Flat JSON array, ~3KB |
| Coordinates | `bounds="[141,1422][1026,1558]"` (need parsing) | `{"x":141,"y":1422,"width":885,"height":136}` (ready to use) |
| Nesting | Deep tree structure | Flat list |
| Labels | Separate `text`, `content-desc`, `resource-id` attributes | Unified `text`, `label`, `identifier` fields |
| LLM tokens | ~14,000 tokens (too big) | ~1,500 tokens (manageable) |

---

## Answered Research Questions

1. **How does mobile-mcp return element data?** — ANSWERED: Clean flat JSON with type, text, label, identifier, coordinates. ~1,500 tokens per screen. Highly LLM-friendly.

2. **Coordinate vs element-based interaction?** — ANSWERED: Coordinates only (x, y). `mobile_click_on_screen_at_coordinates` takes x, y pixels. Agent calculates center from coordinates object. Reliable on same device, may vary across screen sizes.

3. **How to detect errors in mobile apps?** — PARTIALLY ANSWERED: Call `mobile_list_elements_on_screen` after an action and look for new TextViews with error text. No JS equivalent — must re-scan the screen. Still need to test what error messages look like in the bank app.

4. **How to handle scrolling?** — UNANSWERED: Need to test `mobile_swipe_on_screen` and see if elements below the fold appear in subsequent `list_elements_on_screen` calls.

5. **App state management?** — ANSWERED: `mobile_terminate_app` + `mobile_launch_app` resets the app. For clearing a field: long press → select all → type new text (no clear_text tool).

6. **Performance?** — PARTIALLY ANSWERED: `list_elements_on_screen` responded in <2 seconds during testing. Similar to web `take_snapshot`.

7. **Bank app specific:** — ANSWERED: Good accessibility labels on all interactive elements. `content-desc` / `label` populated for dropdowns, buttons, inputs. Some elements missing `identifier` but all have `label` or `text`.

## Verified: Tap + Type Interaction (2026-03-24)

Tested on iTeller form — tapped "Enter Details" field and typed "TestAgent123". All free, no LLM.

### Results:
```
TAP:    mobile_click_on_screen_at_coordinates(x=583, y=1490) → "Clicked on screen"
TYPE:   mobile_type_keys(text="TestAgent123", submit=false)   → "Typed text: TestAgent123"
VERIFY: mobile_list_elements_on_screen → text changed from "Enter Details" to "TestAgent123" ✅
```

### Key discovery: Coordinates shift after keyboard appears
Before keyboard: `EditText` at y=1422
After keyboard:  `EditText` at y=1208 (shifted up by 214px)

**Impact:** Agent must re-scan (`list_elements_on_screen`) after tapping a field because the keyboard pushes elements up. Can't use pre-computed coordinates for elements below the tapped field.

### Interaction pattern for mobile (2 tool calls vs 1 in web):
```
Web:    evaluate_script("el.value = 'test'; el.blur(); checkError()")  → 1 call
Mobile: tap(x, y) → type_keys("test") → list_elements (verify)        → 3 calls
```

---

## Open Questions (Remaining)

1. **Scrolling:** How does the agent discover off-screen elements? Does swiping + re-listing reveal them?
2. **Error detection:** What do validation errors look like in the bank app's a11y tree?
3. **Keyboard dismissal:** Does tapping outside the field dismiss the keyboard, or does the agent need `press_button(BACK)`?
4. **Dropdown interaction:** After tapping "Select Transaction Type", what does the picker look like in the a11y tree?
5. **Cross-device coordinates:** Will the same coordinates work on a different phone with different screen resolution?
6. **Clearing text:** No `clear_text` tool — need to test: triple-tap (select all) then type, or long press → select all → type?

---

## Progress

- [x] Install ADB (`brew install android-platform-tools`) — DONE
- [x] Connect Android phone, enable USB debugging, verify with `adb devices` — DONE (device: RZCXA21GV9P, SM-M356B, Android 16)
- [x] Install mobile-mcp (`npx -y @mobilenext/mobile-mcp@latest`) — DONE (v0.0.48, runs on stdio)
- [x] Test `mobile_list_elements_on_screen` on the bank app — DONE (clean JSON, ~1,500 tokens, all elements labeled)
- [x] Verify ADB raw dump vs mobile-mcp output — DONE (mobile-mcp is significantly cleaner)
- [x] Test `mobile_click_on_screen_at_coordinates` + `mobile_type_keys` on a form field — DONE (tap + type + verify all work)
- [ ] Test dropdown interaction (tap → picker → select → verify)
- [ ] Test scrolling (swipe → list elements → find new elements)
- [ ] Design mobile-specific prompts based on verified output format
- [ ] Build POC: agent fills one screen of the bank app
- [ ] Test Pass 2 flow: plan test cases → execute on mobile
