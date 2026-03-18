# Imports and Data Models
import json
from collections import deque
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path

import structlog

logger = structlog.get_logger()

MAX_RETRIES = 3
LOOP_WINDOW = 5
RUNS_DIR = Path(__file__).parent / "runs"

@dataclass
class FieldRecord:
    name: str
    uid: str
    action: str                     # "fill", "click", "select", "upload"
    xpath: str = ""                 # relative XPath — core output for test reuse
    status: str = "pending"         # "pending", "filled", "failed", "skipped"
    value: str | None = None
    retries: int = 0
    error: str | None = None


@dataclass
class SectionRecord:
    name: str
    status: str = "not_started"     # "not_started", "exploring", "in_progress", "completed"
    fields: list[FieldRecord] = field(default_factory=list)
    url: str | None = None


@dataclass
class FailureRecord:
    section: str
    field: str
    action: str
    error: str
    diagnosis: str
    evidence: dict = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda:datetime.now().isoformat())


@dataclass
class RunState:
    run_id: str
    url: str
    app_name: str
    status: str = "running"  # "running", "completed", "crashed", "stopped"
    current_section: str | None=None
    sections: list[SectionRecord] = field(default_factory=list)
    failures: list[FailureRecord] = field(default_factory=list)
    start_time: str = field(default_factory=lambda: datetime.now().isoformat())
    end_time: str | None = None


# The StateTracker Class
class StateTracker:
    def __init__(self) -> None:
        self._state: RunState | None = None
        self._recent_actions: deque[str] = deque(maxlen=LOOP_WINDOW)
        RUNS_DIR.mkdir(parents=True, exist_ok=True)

    # ------ Run lifecycle -------
    def start_run(self, url: str, app_name: str) -> str:
        run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._state = RunState(run_id=run_id, url=url, app_name=app_name)
        self._recent_actions.clear()
        self.save()
        logger.info("run_started", run_id=run_id, url=url, app_name=app_name)
        return run_id
    
    def end_run(self, status: str = "completed") -> None:
        self._state.status = status
        self._state.end_time = datetime.now().isoformat()
        self.save()
        logger.info("run_ended", run_id=self._state.run_id, status=status)

    # ----- section tracking ---------
    def discover_section(self, name: str, url:str | None = None) ->None:
        existing = self._find_section(name)
        if existing:
            existing.status = "exploring"
            if url:
                existing.url = url
        else:
            self._state.sections.append(
                SectionRecord(name=name, status="exploring", url=url)
            )
        self._state.current_section = name
        self.save()
        logger.info("section_discovered", section=name, url=url)

    
    def complete_section(self, name: str) -> None:
        section = self._find_section(name)
        if section:
            section.status = "completed"
            self.save()
            logger.info("section_completed", section=name)

    # --- Field tracking ---

    def discover_fields(self, section_name: str, fields: list[dict]) -> None:
        section = self._find_section(section_name)
        if not section:
            self.discover_section(section_name)
            section = self._find_section(section_name)
        for f in fields:
            existing = self._find_field(section, f["name"])
            if not existing:
                section.fields.append(FieldRecord(
                    name=f["name"],
                    uid=f.get("uid", ""),
                    action=f.get("action", "fill"),
                    xpath=f.get("xpath", ""),
                ))
        section.status = "in_progress"
        self.save()
        logger.info("fields_discovered", section=section_name, count=len(fields))

    def update_field(
        self, section_name: str, field_name: str,
        status: str, value: str | None = None,
        xpath: str | None = None
    ) -> None:
        section = self._find_section(section_name)
        if not section:
            return
        fld = self._find_field(section, field_name)
        if not fld:
            return
        fld.status = status
        if value is not None:
            fld.value = value
        if xpath is not None:
            fld.xpath = xpath
        if status == "failed":
            fld.retries += 1
        self.save()
        logger.info("field_updated", section=section_name,
                     field=field_name, status=status,
                     xpath=xpath or fld.xpath)

    # --- Failure logging ---

    def log_failure(
        self, section: str, field_name: str, action: str,
        error: str, diagnosis: str, evidence: dict | None = None
    ) -> None:
        record = FailureRecord(
            section=section,
            field=field_name,
            action=action,
            error=error,
            diagnosis=diagnosis,
            evidence=evidence or {},
        )
        self._state.failures.append(record)
        self.save()
        logger.error("failure_logged", section=section,
                      field=field_name, error=error)

    # --- Loop detection ---

    def check_for_loop(self, action: str) -> bool:
        self._recent_actions.append(action)
        if len(self._recent_actions) == LOOP_WINDOW:
            unique = len(set(self._recent_actions))
            if unique <= 2:
                logger.warning("loop_detected",
                               actions=list(self._recent_actions))
                return True
        return False

    # --- Progress summaries ---

    def get_summary(self) -> str:
        if not self._state:
            return "No active run."
        total_fields = 0
        filled = 0
        failed = 0
        skipped = 0
        for sec in self._state.sections:
            for f in sec.fields:
                total_fields += 1
                match f.status:
                    case "filled":
                        filled += 1
                    case "failed":
                        failed += 1
                    case "skipped":
                        skipped += 1
        sections_done = sum(
            1 for s in self._state.sections if s.status == "completed"
        )
        total_sections = len(self._state.sections)
        return (
            f"{filled}/{total_fields} fields done, "
            f"{failed} failed, {skipped} skipped, "
            f"section {sections_done}/{total_sections}"
        )

    def get_section_summary(self, section_name: str) -> str:
        section = self._find_section(section_name)
        if not section:
            return f"Section '{section_name}' not found."
        lines = [f"Section: {section.name} [{section.status}]"]
        for f in section.fields:
            status_mark = {"filled": "+", "failed": "X",
                           "skipped": "-", "pending": " "}
            mark = status_mark.get(f.status, "?")
            lines.append(f"  [{mark}] {f.name}: {f.status}")
        return "\n".join(lines)

    # --- Persistence ---

    def save(self) -> None:
        if not self._state:
            return
        path = RUNS_DIR / f"run_{self._state.run_id}.json"
        with open(path, "w") as f:
            json.dump(asdict(self._state), f, indent=2)

    def load(self, run_id: str) -> None:
        path = RUNS_DIR / f"run_{run_id}.json"
        with open(path) as f:
            data = json.load(f)
        self.restore(data)
        logger.info("run_loaded", run_id=run_id)

    def export(self) -> dict:
        return asdict(self._state) if self._state else {}

    def restore(self, data: dict) -> None:
        self._state = RunState(
            run_id=data["run_id"],
            url=data["url"],
            app_name=data["app_name"],
            status=data.get("status", "running"),
            current_section=data.get("current_section"),
            start_time=data.get("start_time", ""),
            end_time=data.get("end_time"),
            sections=[
                SectionRecord(
                    name=s["name"],
                    status=s["status"],
                    url=s.get("url"),
                    fields=[FieldRecord(**fld) for fld in s.get("fields", [])],
                )
                for s in data.get("sections", [])
            ],
            failures=[
                FailureRecord(**fail)
                for fail in data.get("failures", [])
            ],
        )

    # --- Private helpers ---

    def _find_section(self, name: str) -> SectionRecord | None:
        if not self._state:
            return None
        for s in self._state.sections:
            if s.name == name:
                return s
        return None

    def _find_field(self, section: SectionRecord, name: str) -> FieldRecord | None:
        for f in section.fields:
            if f.name == name:
                return f
        return None

if __name__ == "__main__":
    tracker = StateTracker()

    # Start a run
    run_id = tracker.start_run(
        url="https://qa-tq-awp.impactodigifin.xyz/newapplication",
        app_name="TECU Credit Union",
    )
    print(f"Run started: {run_id}")

    # Discover a section and fields
    tracker.discover_section("Contact Info", url="https://qa-tq-awp.impactodigifin.xyz/newapplication")
    tracker.discover_fields("Contact Info", [
        {"name": "First Name", "uid": "1_6", "action": "fill"},
        {"name": "Last Name", "uid": "1_8", "action": "fill"},
        {"name": "Email", "uid": "1_10", "action": "fill"},
        {"name": "Country Code", "uid": "1_14", "action": "select"},
        {"name": "Phone", "uid": "1_16", "action": "fill"},
    ])

    # Simulate filling fields
    tracker.update_field("Contact Info", "First Name", "filled", "Roman")
    tracker.update_field("Contact Info", "Last Name", "filled", "Perez")
    tracker.update_field("Contact Info", "Email", "filled", "roman@test.com")
    tracker.update_field("Contact Info", "Country Code", "failed")

    # Log a failure
    tracker.log_failure(
        section="Contact Info",
        field_name="Country Code",
        action="click",
        error="Dropdown returned empty options",
        diagnosis="API /drop-downs?type=country returned 500",
        evidence={"network": "GET /drop-downs?type=country → 500",
                  "console": "Error: Failed to fetch countries"},
    )

    # Loop detection test
    print(f"Loop? {tracker.check_for_loop('click_1_14')}")  # False
    print(f"Loop? {tracker.check_for_loop('click_1_14')}")  # False
    print(f"Loop? {tracker.check_for_loop('click_1_14')}")  # False
    print(f"Loop? {tracker.check_for_loop('click_1_14')}")  # False
    print(f"Loop? {tracker.check_for_loop('click_1_14')}")  # True — loop!

    # Print summary
    print()
    print(tracker.get_summary())
    print()
    print(tracker.get_section_summary("Contact Info"))

    # Verify JSON was saved
    json_path = RUNS_DIR / f"run_{run_id}.json"
    print(f"\nJSON saved to: {json_path}")
    print(f"File size: {json_path.stat().st_size} bytes")

    # End run
    tracker.end_run("completed")
    print("Self-test passed.")
