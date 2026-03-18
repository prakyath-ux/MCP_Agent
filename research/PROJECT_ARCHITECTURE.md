# Regression-Playwright — Complete Project Architecture

**Purpose:** This document explains the entire current project so an AI assistant (Claude) working on a new AI agent project can understand the existing system it's meant to complement/replace.

---

## 1. What This Project Is

A **CSV-driven end-to-end test automation framework** with a Streamlit web UI. QA teams define web UI tests using CSV spreadsheets → the system auto-generates Playwright test scripts → executes them → produces reports.

**The philosophy:** No-code test definition. Humans record clicks, the system does everything else.

**Tech Stack:** Python 3.12, Playwright (browser automation), pytest (test framework), Streamlit (web UI), Allure (reporting)

---

## 2. Project Structure

```
regression-playwright/
├── streamlitApp_V4.0/              # Streamlit web application (3 pages)
│   ├── app.py                      # Page 1: Recording UI + Live View (612 lines)
│   ├── pages/
│   │   ├── 2_Generate_and_Validate.py  # Page 2: LLM edge case gen (disabled)
│   │   └── 3_Launch_Test.py            # Page 3: Test execution + scheduling
│   ├── src/
│   │   ├── recorder.py             # Playwright browser + JS XPath capture (448 lines)
│   │   ├── llm_generator.py        # Groq API for edge cases (574 lines)
│   │   ├── csv_validator.py        # CSV validation (199 lines)
│   │   └── validator.py            # Test data validation (331 lines)
│   ├── data/
│   │   ├── captures/               # Recorded element XPaths (.jsonl, .json, .csv)
│   │   ├── samples/                # Sample test CSVs
│   │   └── scheduler/              # APScheduler jobs + history
│   └── .streamlit/config.toml      # Streamlit config
│
├── tests/                          # Auto-generated test scripts
│   ├── conftest.py                 # Pytest hooks + auto-regeneration from CSV
│   └── test_*.py                   # Generated test files (one per CSV)
│
├── test_csv/                       # CSV test specifications (INPUT)
│   ├── E2E_test_1.csv              # 153 steps, 6 page groups
│   ├── E2E_test_2.csv
│   ├── E2E_test_3.csv
│   └── E2E_test_4.csv              # 170+ steps
│
├── locators/                       # Auto-generated XPath locator classes
│   └── *_locators.py               # One per CSV (grouped by page section)
│
├── pages/                          # Page Object Model
│   └── base_page.py                # Core action library — 40+ methods (849 lines)
│
├── fixtures/                       # Pytest browser/device configuration
│   ├── playwright_fixtures.py      # Main fixtures (496 lines)
│   ├── fixture_sample.py           # Reference examples (348 lines)
│   ├── geo_browser_fixture.py      # Geolocation examples
│   └── proxy_fixture.py            # Proxy examples
│
├── utils/                          # Utilities
│   ├── test_generator.py           # CSV → test script generator (500+ lines)
│   ├── config.py                   # BASE_URL, credentials
│   ├── helpers.py                  # Random email/mobile generators
│   ├── logger.py                   # Logging config
│   ├── file_helpers.py             # CSV/Excel/JSON I/O
│   └── locator_converter.py        # XPath format conversion
│
├── resources/                      # Test assets
│   ├── testdata.json               # Test credentials
│   └── test_files/                 # Upload files (PNG, JPG for ID docs, bills)
│       ├── AVINASH_PASS.png        # Passport scan
│       ├── DAVID_N_F.png           # ID front
│       ├── DAVID_N_B.png           # ID back
│       ├── FlowBill.jpg            # Utility bill
│       └── pp.jpg                  # Profile picture
│
├── reports/allure-results/         # Allure test reports (generated)
├── excel_report/                   # Excel reports (generated)
├── videos/                         # Test video recordings (generated)
├── live_screenshots/               # Live screenshot feed during tests
│
├── pytest.ini                      # Pytest settings (188 lines)
├── requirements.txt                # Python dependencies
├── conftest.py                     # Root pytest config
└── .gitignore
```

---

## 3. The Three Workflows

### 3.1 Recording (Page 1 — app.py)

```
User enters URL in Streamlit → clicks "Start Recording"
      │
      ▼
Subprocess launches recorder.py with URL
      │
      ▼
Playwright opens a headed Chrome browser (visible window)
JavaScript injected into page for XPath capture
      │
      ▼
User clicks elements on the web page
      │
      ▼
Each click/input captured as JSON:
{label, xpath, strategy, matches, action, values, property, timestamp}
Written to .live_capture.jsonl (one entry per line, real-time)
      │
      ▼
Streamlit reads .jsonl and displays live table (2-second refresh)
User assigns page groups (Contact Info, Documents, etc.)
      │
      ▼
User clicks "Stop Recording"
Data saved to xpaths_<timestamp>.json + .csv
User downloads CSV
```

**The JavaScript XPath Strategy (recorder.py lines 23-277):**
11-tier priority for generating XPaths:
1. `@id` attribute (most reliable)
2. `@name` attribute
3. `@data-testid`
4. `@aria-label`
5. `@role`
6. `@placeholder`
7. `type+name` for inputs
8. `href` for links
9. Class names
10. Text content
11. Absolute path (fallback)

**Known capture patterns requiring cleanup:**
- **File uploads** create 3 events (click label → click hidden input → set value) → deduplicated to 1 row at download time via `clean_upload_duplicates()` in app.py
- **Radio/checkbox with styled wrappers** create 2-3 events (div click → input click → value) → merged to 1 row at download time

---

### 3.2 Test Generation (automatic on pytest run)

```
CSV placed in test_csv/ folder
      │
      ▼
pytest starts → conftest.py pytest_configure hook fires
      │
      ▼
Scans test_csv/ for all .csv files
For each CSV:
      │
      ├── detect_csv_format() → horizontal or vertical?
      │
      ├── read_horizontal_csv() or read_vertical_csv()
      │   Parses rows: Steps, Group, Elements, Property, Action, XPath, Value
      │
      └── generate_from_csv()
            ├── Generates: tests/test_<name>.py
            └── Generates: locators/<name>_locators.py
```

**CSV Format — Horizontal (what we use):**
```
Steps    │ 1           │ 2           │ 3        │ ...
Group    │ Contact Info│ Contact Info│ Contact  │ ...
Elements │ firstName   │ email       │ phone    │ ...
Property │ text        │ email       │ tel      │ ...
Action   │ click       │ Input       │ click    │ ...
XPath    │ //*[@id="."]│ //*[@id="."]│ //*[...] │ ...
Value    │             │ ROMAN       │          │ ...
```

Each column = one test step. Row 1 = step numbers, Row 2 = page group, etc.

**Generated test file structure:**
```python
@allure.feature("test_e2e_test_1")
def test_e2e_test_1(profile_page, user_data):
    bp = BasePage(profile_page)
    bp.navigate("https://qa-tq-awp.impactodigifin.xyz/newapplication")

    with allure.step("Contact Info"):
        bp.click(ContactInfoLocators.FIRSTNAME)
        bp.type(ContactInfoLocators.FIRSTNAME, 'ROMAN')
        bp.click(ContactInfoLocators.EMAIL)
        bp.type(ContactInfoLocators.EMAIL, user_data['email'])
        # ... more steps

    with allure.step("Documents"):
        # ... document upload steps
```

**Generated locator file structure:**
```python
class ContactInfoLocators:
    FIRSTNAME = '//*[@id="firstName"]'
    EMAIL = '//*[@id="email"]'
    MOBILENUMBER = '//*[@id="mobileNumber"]'

class DocumentsLocators:
    CHOOSE_AN_OPTION = '//button[.="Choose an option"]'
    UPLOAD_FRONT_INPUT = '//*[@id="upload-front-input"]'
```

---

### 3.3 Test Execution (Page 3 — 3_Launch_Test.py)

```
User uploads CSV or selects existing test in Streamlit Page 3
      │
      ▼
pytest runs the auto-generated test file
      │
      ▼
Fixtures set up:
  - browser_instance (session-scoped, shared across tests)
  - profile_page (persistent Chrome profile with cookies/cache)
  - user_data (random email + mobile per iteration)
      │
      ▼
Test executes step-by-step:
  For each action in CSV:
    1. Wait for element (attached + visible)
    2. Execute action (click / type / upload / keyboard)
    3. Take screenshot (Allure attachment)
    4. Record result (PASS / FAIL / SKIP)
    5. Write action label to live_screenshots/latest_action.txt
      │
      ▼
Reports generated:
  - Allure HTML report (screenshots + videos per step)
  - Excel report (color-coded: green=PASS, red=FAIL, yellow=SKIP)
  - Video recording (.webm)
```

---

## 4. Core Components — Deep Dive

### 4.1 BasePage (pages/base_page.py — 849 lines)

The heart of the framework. Wraps Playwright's `page` object with 40+ methods. Every method:
- Is an Allure step (auto-logged)
- Takes screenshots
- Handles waiting (attached → visible → optional enabled)
- Records PASS/FAIL/SKIP result

**Key methods:**

| Category | Methods |
|----------|---------|
| Navigation | `navigate(url)`, `wait_for_load_state()`, `wait_for_url()` |
| Click/Input | `click(locator)`, `type(locator, text)`, `select_option()`, `check()`, `uncheck()` |
| File Upload | `upload_file(locator, file_path)` |
| Keyboard | `keyboard_type(locator, text)` — character-by-character (for date pickers) |
| Waits | `wait_for_visible()`, `wait_for_hidden()`, `wait_for_attached()`, `wait_for_text()`, `wait_for_enabled()` |
| Assertions | `assert_visible()`, `assert_text_equals()`, `assert_text_contains()`, `assert_url_contains()` |
| State checks | `is_visible()`, `is_enabled()`, `is_checked()`, `get_attribute()`, `get_element_count()` |
| Browser | `clear_cache()`, `clear_cookies()`, `clear_all()` |
| Screenshots | `take_screenshot()`, `take_element_screenshot()` |
| Reports | `generate_report()`, `_write_excel_report()`, `_write_csv_report()` |

**XPath handling:**
- Auto-prepends `xpath=` prefix for absolute XPaths (starting with `/`)
- Handles multi-element ambiguity by adding `[1]` index
- Normalizes locator strings

---

### 4.2 Fixtures (fixtures/playwright_fixtures.py — 496 lines)

**Chrome launch args:**
```python
CHROME_ARGS = [
    "--disable-gpu",
    "--disable-popup-blocking",
    "--start-maximized",
    "--incognito",
    "--disable-notifications",
    "--remote-debugging-port=9222",    # CDP for live screenshots
    "--remote-allow-origins=*",
]
```

**Key fixtures:**

| Fixture | Scope | Purpose |
|---------|-------|---------|
| `browser_instance` | session | Shared browser across all tests |
| `context` | function | Fresh context per test |
| `page` | function | Fresh page per test |
| `profile_page` | session | Persistent Chrome profile (cookies/cache preserved) |
| `mobile_page` | function | Mobile device emulation |
| `user_data` | function | Random email + mobile number |

**CLI options:**
```bash
pytest --proxy=URL              # Direct proxy
pytest --proxy-mode=rotating    # Rotating proxy pool
pytest --geo=US                 # Geolocation emulation
pytest --mobile=Pixel           # Mobile device
pytest --user-profile=PATH      # Persistent profile
pytest --auto-recovery-browser  # Auto-reopen if browser closes
pytest --record-video           # Record test video
```

**Mobile presets:** Pixel (393x851), iPhone (390x844), Galaxy S21 (360x800), iPad (820x1180)

**Geo presets:** US (San Francisco), UK (London), India (Mumbai), Germany (Berlin)

---

### 4.3 Test Generator (utils/test_generator.py — 500+ lines)

**What it does:**
1. Reads CSV (horizontal or vertical format)
2. Detects format by scanning first 8 rows for markers (group, element, action, xpath, value, property, strategy, steps)
3. Generates Python test file with Allure step groups
4. Generates locator class file grouped by page section

**Format detection:**
- 3+ markers found → horizontal format
- Otherwise → vertical format

**Special handling:**
- File upload steps: generates `_skip_step()` calls for intermediate click rows
- Random data: injects `user_data['email']` and `user_data['mobile']` where CSV has `random`
- Edge case parameterization: supports multiple test data iterations

---

### 4.4 Test CSV Format (what the AI agent would replicate)

**E2E_test_1.csv — 153 steps across 6 pages:**

| Page Group | Steps | What happens |
|-----------|-------|-------------|
| Contact Info | 1-23 | First name, last name, email, phone, branch selection, profile pic upload, OTP entry |
| Documents | 24-53 | ID card upload (front+back), passport, utility bill, authorization |
| Additional Details | 54-93 | Employment status, employer, occupation, dates, sector, income, personal details, marital status |
| Other Products | 94-124 | Bank selection, account number, beneficiary, member search |
| PEP/FATCA | 125-141 | 17 yes/no political exposure questions (all toggle clicks) |
| PDF/Other Details | 142-153 | Document uploads (payslip, job letter), final OTP, submit |

**Action breakdown:**
- ~104 click actions (dropdown opens, option selects, toggles, buttons, navigation)
- ~47 Input actions (typing text, file paths, OTP digits)
- 2 keyboardinput actions (date picker year entry)

**CSV rows:**
```
Row 1: Steps           → 1, 2, 3, ... 153
Row 2: Group           → Contact Info, Contact Info, ... PDF/Other Details
Row 3: Elements        → firstName, email, mobileNumber, ...
Row 4: Property        → text, email, tel, submit, li, label, file, ...
Row 5: Action          → click, Input, keyboardinput
Row 6: XPath           → //*[@id="firstName"], //button[.="Choose an option"], ...
Row 7: Value           → ROMAN, random, D:\path\to\file.jpg, ...
Row 8: Expected_Result → PASS, PASS, SKIP, ...
```

---

## 5. The Application Being Tested

**URL:** `https://qa-tq-awp.impactodigifin.xyz/newapplication`
**What:** TECU Credit Union — Loan Application Form
**Structure:** Multi-page wizard with Save & Continue navigation

**Pages/Sections:**
1. **Contact Info** — Name, email, phone, branch dropdown, profile picture, OTP verification
2. **Documents** — ID uploads (National ID front/back, Passport, Utility Bill), authorization upload
3. **Additional Details** — Employment (status, employer, occupation, dates, sector, type), income, address, country, personal (nationality, birth, marital status, education, communication preference)
4. **Other Products** — Bank details, account info, beneficiary setup with member search, terms acceptance
5. **PEP/FATCA** — Political Exposure Person declarations (17 yes/no toggles)
6. **PDF/Other Details** — Final document uploads (payslip, job letter), submission OTP

**UI patterns:**
- Dropdowns load options from API calls (`/drop-downs` endpoint)
- File uploads use hidden `<input type="file">` with styled label buttons
- OTP entry: 6 individual digit inputs (`otp-input-0` through `otp-input-5`) + Verify button
- Radio/checkbox: styled div wrappers hiding actual `<input>` elements
- Date pickers: keyboard input for year field (`:rt:-year`, `:r1d:-year`)
- Cascading dropdowns: some options depend on previous selections

---

## 6. Infrastructure & Deployment

### Server: 172.16.0.146

**Two instances running on this server:**

| | Main App (production, client testing) | Dev/Testing App |
|--|--------------------------------------|-----------------|
| Path | `/home/dev/project/reg_test_v2/regression-playwright/` | `/home/dev/project/testing_features_QA/regression-playwright/` |
| Branch | `dev3_146_server` | `dev-features` |
| Streamlit port | 8501 (default) | 8502 |
| Xvfb display | :1 | :2 |
| noVNC port | 6080 | 6081 |
| x11vnc port | 5900 | 5901 |

**IMPORTANT:** The main production app is used for CLIENT TESTING. Never touch or conflict with it.

### How headless browser works on the server

```
Xvfb (Virtual Framebuffer)
  Creates a virtual screen (:1 or :2) — no physical monitor needed
        │
        ▼
Playwright launches Chrome targeting the virtual display
  DISPLAY=:1 chromium-browser
  Chrome renders on the virtual screen (invisible to humans)
        │
        ▼
x11vnc (VNC Server)
  Mirrors the virtual display over VNC protocol
  Listens on port 5900/5901
        │
        ▼
websockify/noVNC (Web-based VNC viewer)
  Converts VNC to WebSocket for browser access
  Listens on port 6080/6081
        │
        ▼
Users open: http://172.16.0.146:6080/vnc.html?autoconnect=true
  See the browser in real-time from any web browser
```

**Start commands (using nohup for persistence):**
```bash
# Virtual display
nohup Xvfb :2 -screen 0 1920x1080x24 &

# VNC server (with -shared for multiple viewers)
nohup x11vnc -display :2 -forever -nopw -rfbport 5901 -shared &

# Web VNC viewer
nohup websockify --web /usr/share/novnc 6081 localhost:5901 &

# Streamlit app
nohup python -m streamlit run streamlitApp_V4.0/app.py --server.port 8502 &
```

### Server: 172.16.0.55 (development)

Same stack as .146 but for local development. Uses default ports (8501, 6080, 5900, display :1).

---

## 7. Dependencies

```
# Browser automation
playwright>=1.48.0
pytest
pytest-playwright
allure-pytest

# Web UI
streamlit>=1.32.0
pandas>=2.0.0
numpy
matplotlib
openpyxl>=3.1.0

# Utilities
faker                          # Fake data generation
APScheduler==3.10.4            # Scheduled test runs
websocket-client               # WebSocket support
dotenv                         # Environment variables

# LLM (currently unused in production)
google-generativeai            # Gemini API
groq                           # Groq API (edge case generation)
```

**Python environment:** `iborg12` (conda/venv)

---

## 8. How a Test Run Looks End-to-End

```
1. Human creates CSV (or records via Streamlit Page 1)
   ↓
2. CSV placed in test_csv/ folder
   ↓
3. pytest starts
   ↓
4. conftest.py auto-generates test script + locator classes from CSV
   ↓
5. Fixtures launch Chrome (headed, with CDP on port 9222)
   ↓
6. Test navigates to https://qa-tq-awp.impactodigifin.xyz/newapplication
   ↓
7. For each CSV step:
   - BasePage.click() or .type() or .upload_file() or .keyboard_type()
   - Each action: wait for element → perform action → screenshot → record result
   ↓
8. After all steps: generate Excel report (color-coded)
   ↓
9. Allure captures everything: screenshots, videos, step metadata
   ↓
10. Reports available in reports/allure-results/ and excel_report/
```

**Speed:** ~30-60 seconds for 153 steps (no LLM, just script replay)
**Cost:** $0 per run (no API calls)
**Maintenance:** HIGH — any UI change requires re-recording CSV or manually editing XPaths

---

## 9. Key Design Patterns

1. **CSV-Driven Testing** — Test logic in spreadsheets, not code
2. **Auto-Generation** — Tests regenerated from CSV on every pytest run
3. **Page Object Model** — Centralized BasePage with reusable action methods
4. **Session-Scoped Browser** — One browser shared across tests (fast, but fragile)
5. **Allure Step Integration** — Every BasePage method is an Allure step
6. **Live Feedback** — Real-time screenshot feed + live capture display
7. **Deduplication at Download** — Raw captures kept intact, cleaned on export

---

## 10. What This Project CANNOT Do (Why We're Building the AI Agent)

| Limitation | Detail |
|-----------|--------|
| **No intelligence** | Replays CSV blindly — doesn't understand WHAT it's testing |
| **Brittle XPaths** | One UI change (renamed ID, moved element) breaks tests |
| **No self-healing** | If a step fails, it stops or records FAIL — doesn't try alternatives |
| **No diagnosis** | Reports THAT something failed, not WHY |
| **Manual recording** | Human must click every element to build the CSV (30-60 minutes) |
| **Manual data** | Test values hardcoded in CSV or randomly generated without context |
| **No adaptation** | Can't handle new fields, changed layouts, or dynamic content |
| **Maintenance burden** | Every UI update requires re-recording or CSV editing |

**The AI agent aims to solve all of these** by using an LLM (Claude/Gemini) + Chrome DevTools MCP to dynamically explore, understand, and test the application — adapting in real-time like a human QA tester.
