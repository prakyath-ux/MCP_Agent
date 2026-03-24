# Mobile Agent — Build Guide

Step-by-step instructions to set up and run the mobile testing agent from scratch.

---

## Prerequisites

- macOS (tested on Darwin 25.2.0, Mac Mini M-series)
- Python 3.10+
- Node.js v20+
- USB cable (USB-C to USB-C or USB-A to USB-C depending on phone)
- Android phone with the app to test installed

---

## Step 1: Install ADB

ADB (Android Debug Bridge) lets your Mac communicate with the Android phone.

```bash
brew install android-platform-tools
```

Verify:
```bash
adb version
# Expected: Android Debug Bridge version 1.x.x
```

---

## Step 2: Enable USB Debugging on Android Phone

1. Open **Settings** on the phone
2. Go to **About Phone**
3. Tap **Build Number** 7 times — you'll see "You are now a developer"
4. Go back to **Settings → Developer Options**
5. Enable **USB Debugging**
6. Connect phone to Mac via USB cable
7. A popup appears on phone: "Allow USB debugging?" → Tap **Allow** and check **"Always allow from this computer"**

---

## Step 3: Verify Phone Connection

```bash
adb devices
```

Expected output:
```
List of devices attached
RZCXA21GV9P     device
```

If it shows `unauthorized` instead of `device`:
1. Unplug USB cable
2. On phone: Settings → Developer Options → **Revoke USB debugging authorizations**
3. Plug cable back in
4. Tap **Allow** on the popup
5. Run `adb devices` again

---

## Step 4: Install mobile-mcp

```bash
npx -y @mobilenext/mobile-mcp@latest
```

Expected output:
```
mobile-mcp server running on stdio
```

Press `Ctrl+C` to stop — we don't run it manually. The orchestrator launches it.

---

## Step 5: Project Setup

```bash
cd /Users/impactoinfra/MCP_agent

# Virtual environment (reuse existing)
source venv/bin/activate

# Install any new dependencies if needed
pip install -r requirements.txt
```

---

## Step 6: Test mobile-mcp Connection (Free, No LLM)

Run the test script to verify mobile-mcp can see your phone and read the screen:

```bash
cd /Users/impactoinfra/MCP_agent && python mobile_version/test_mcp.py
```

Expected output:
```
=== AVAILABLE TOOLS ===
  mobile_list_available_devices
  mobile_list_elements_on_screen
  ... (21 tools total)

=== DEVICES ===
  {"devices":[{"id":"RZCXA21GV9P","name":"SM-M356B",...}]}

=== ELEMENTS ON SCREEN ===
  [{"type":"android.widget.EditText","text":"Enter Details",...}, ...]
```

**Important:** The phone must be unlocked with the target app visible on screen.

If `ELEMENTS ON SCREEN` returns data, mobile-mcp is working. The output is clean JSON with element type, text, label, and coordinates — this is what the LLM agent will read.

**Note:** Update the `device` ID in `test_mcp.py` line 31 if your phone ID changes.

---

## Step 7: Find the App Package Name

To launch an app via mobile-mcp, you need its Android package name.

```bash
# List all installed packages
adb shell pm list packages | grep -i "bank\|tecu\|finance"

# Or list third-party apps only (non-system)
adb shell pm list packages -3
```

Note the package name (e.g., `com.example.bankapp`).

---

## Step 8: Run the Mobile Agent

```bash
cd /Users/impactoinfra/MCP_agent/mobile_version

# Recon — explore app screen, list elements (cheap, ~$0.05)
python run.py recon "DEVICE_ID" "com.example.bankapp" "Bank App"

# POC — explore + fill fields + extract element data (~$0.15-0.30)
python run.py poc "DEVICE_ID" "com.example.bankapp" "Bank App"

# Test cases — plan + execute tests (~$0.30-0.40)
python run.py testcase "DEVICE_ID" "com.example.bankapp" "Bank App"
```

Replace `DEVICE_ID` with your phone's ID from `adb devices` (e.g., `RZCXA21GV9P`).

---

## Troubleshooting

### "adb: command not found"
```bash
brew install android-platform-tools
```

### "device unauthorized"
Revoke USB debugging on phone, replug cable, tap Allow.

### "mobile-mcp server running on stdio" then nothing happens
That's normal — it's waiting for MCP protocol input. Press Ctrl+C. The orchestrator handles this.

### Orphan processes after Ctrl+C
```bash
# Kill any stuck mobile-mcp or adb processes
pkill -f mobile-mcp
adb kill-server
adb start-server
```

### Phone screen locked during test
Unlock the phone before running. Or disable screen lock:
Settings → Security → Screen Lock → None (for testing only)

### "error: no devices/emulators found"
1. Check USB cable is connected
2. Check USB Debugging is enabled
3. Run `adb kill-server && adb start-server && adb devices`

---

## File Structure

```
mobile_version/
├── research.md          # R&D findings, tool comparisons, decisions
├── build.md             # This file — setup instructions
├── config.py            # Constants, pricing, turn limits (TODO)
├── prompts.py           # Mobile-specific system prompts (TODO)
├── orchestrator.py      # Turn loop with mobile-mcp (TODO)
├── run.py               # CLI entrypoint (TODO)
├── knowledge/           # Saved element data per app screen (TODO)
├── runs/                # Output files per run (TODO)
│   ├── pass1/
│   └── pass2/
└── reports/             # Test reports (TODO)
```

---

## Key Differences from Web Agent (version_2)

| What | Web (version_2) | Mobile (mobile_version) |
|------|----------------|------------------------|
| MCP Server | `chrome-devtools-mcp` | `@mobilenext/mobile-mcp` |
| Element IDs | UIDs from snapshot | Coordinates (x, y) from a11y tree |
| Text input | `fill(uid, value)` — 1 step | `tap(x, y)` + `type_keys(text)` — 2 steps |
| Error checking | `evaluate_script` reads DOM | `list_elements_on_screen` reads a11y tree |
| Navigation | URLs | App package names + screen flows |
| Scrolling | Automatic | Manual `swipe` needed |
| Evidence | Text output only | Screenshots + screen recording available |

---

## Current Status

- [x] ADB installed
- [x] Phone connected (RZCXA21GV9P, SM-M356B, Android 16)
- [x] mobile-mcp installed and verified (v0.0.48, 21 tools)
- [x] Test `mobile_list_elements_on_screen` output — clean JSON, ~1,500 tokens per screen
- [x] Bank app a11y tree verified — good labels, identifiers on key elements
- [x] Test tap + type interaction on a form field — DONE (TestAgent123 entered successfully)
- [ ] Test dropdown interaction
- [ ] Build mobile orchestrator
- [ ] Build mobile prompts
- [ ] POC: fill one screen of bank app
