"""
Delivery Workflow Engine

Core state machine logic for the product-delivery lifecycle. Deterministic:
owns schema validation, legal transitions, optimistic concurrency, idempotency,
guard evaluation, evidence recording, and event persistence.

All functions accept a project_dir Path and return structured dicts.
Errors are raised as WorkflowError subclasses, never sys.exit.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

WORKFLOW_DIR = ".workflow"
STATE_FILE = "state.json"
EVENTS_FILE = "events.jsonl"
CONFIG_FILE = "config.json"
EVIDENCE_DIR = "evidence"

SCHEMA_VERSION = "1.0.0"

TOP_LEVEL_STATES = [
    "intake", "discover", "frame", "plan", "deliver",
    "release", "stabilize", "closed", "blocked", "aborted",
]

ACTIVE_STATES = ["intake", "discover", "frame", "plan", "deliver", "release", "stabilize"]
TERMINAL_STATES = ["closed", "aborted"]

WORK_ITEM_STATES = [
    "ready", "implementing", "verifying", "reviewing",
    "accepted", "blocked", "rework", "waived", "cancelled",
]
WORK_ITEM_TERMINAL = ["accepted", "waived", "cancelled"]

VALID_TRANSITIONS = {
    "intake":    ["discover", "blocked", "aborted"],
    "discover":  ["frame", "blocked", "aborted"],
    "frame":     ["plan", "blocked", "aborted"],
    "plan":      ["deliver", "blocked", "aborted"],
    "deliver":   ["release", "blocked", "aborted"],
    "release":   ["stabilize", "deliver", "blocked", "aborted"],
    "stabilize": ["closed", "deliver", "blocked", "aborted"],
}

VALID_ITEM_TRANSITIONS = {
    "ready":        ["implementing"],
    "implementing": ["verifying", "blocked"],
    "verifying":    ["reviewing", "rework"],
    "reviewing":    ["accepted", "rework", "waived"],
    "rework":       ["implementing"],
    "blocked":      ["implementing"],
}


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class WorkflowError(Exception):
    pass

class WorkflowNotFoundError(WorkflowError):
    pass

class WorkflowExistsError(WorkflowError):
    pass

class InvalidTransitionError(WorkflowError):
    pass

class GuardFailedError(WorkflowError):
    def __init__(self, message, guard_results=None):
        super().__init__(message)
        self.guard_results = guard_results or {}

class InvalidPhaseError(WorkflowError):
    pass

class ItemNotFoundError(WorkflowError):
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _generate_workflow_id():
    return f"wf-{uuid.uuid4().hex[:8]}"

def _generate_event_id():
    return f"evt-{uuid.uuid4().hex[:12]}"

def _now_iso():
    return datetime.now(timezone.utc).isoformat()

def _derive_status(phase):
    if phase in TERMINAL_STATES:
        return "terminal"
    if phase == "blocked":
        return "blocked"
    return "active"

def _wf_path(project_dir: Path):
    return project_dir / WORKFLOW_DIR

def _evidence_dir(project_dir: Path) -> Path:
    return _wf_path(project_dir) / EVIDENCE_DIR

def _load_state(project_dir: Path) -> dict:
    state_path = _wf_path(project_dir) / STATE_FILE
    if not state_path.exists():
        raise WorkflowNotFoundError(
            f"No workflow found at {state_path}. Initialize one first."
        )
    with open(state_path) as f:
        return json.load(f)

def _save_state(state: dict, project_dir: Path):
    state["updated_at"] = _now_iso()
    with open(_wf_path(project_dir) / STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
        f.write("\n")

def _append_event(event: dict, project_dir: Path):
    with open(_wf_path(project_dir) / EVENTS_FILE, "a") as f:
        f.write(json.dumps(event) + "\n")

def _load_events(project_dir: Path) -> list:
    events_path = _wf_path(project_dir) / EVENTS_FILE
    if not events_path.exists():
        return []
    events = []
    with open(events_path) as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events

def _load_config(project_dir: Path) -> dict:
    config_path = _wf_path(project_dir) / CONFIG_FILE
    if config_path.exists():
        with open(config_path) as f:
            return json.load(f)
    return {}

def _make_idempotency_key(state, target, item_id=None):
    wf_id = state["workflow_id"]
    rev = state["revision"]
    phase = state["phase"]
    if item_id:
        return f"{wf_id}:{rev}:{item_id}:{phase}-to-{target}"
    return f"{wf_id}:{rev}:{phase}-to-{target}"

def _check_idempotency(key, project_dir: Path) -> bool:
    for e in _load_events(project_dir):
        if e.get("idempotency_key") == key:
            return True
    return False

def _next_item_id(state):
    existing = list(state.get("work_items", {}).keys())
    if not existing:
        return "WI-001"
    nums = [int(k.split("-")[1]) for k in existing]
    return f"WI-{max(nums) + 1:03d}"

def _next_evidence_key(state):
    existing = [e.get("key", "") for e in state.get("evidence_index", [])]
    nums = []
    for key in existing:
        m = re.match(r"EVD-(\d+)", key or "")
        if m:
            nums.append(int(m.group(1)))
    n = max(nums) + 1 if nums else 1
    return f"EVD-{n:03d}"

def _next_ac_id(state):
    existing = [ac.get("id", "") for ac in state.get("acceptance_criteria", [])]
    nums = []
    for key in existing:
        m = re.match(r"AC-(\d+)", key or "")
        if m:
            nums.append(int(m.group(1)))
    n = max(nums) + 1 if nums else 1
    return f"AC-{n:03d}"

def _sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()

def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())

def _work_items_summary(state):
    items = state.get("work_items", {})
    return {
        "total": len(items),
        "accepted": sum(1 for i in items.values() if i["state"] == "accepted"),
        "waived": sum(1 for i in items.values() if i["state"] == "waived"),
        "cancelled": sum(1 for i in items.values() if i["state"] == "cancelled"),
        "blocked": sum(1 for i in items.values() if i["state"] == "blocked"),
        "in_progress": sum(
            1 for i in items.values()
            if i["state"] in ("implementing", "verifying", "reviewing", "rework")
        ),
        "ready": sum(1 for i in items.values() if i["state"] == "ready"),
    }


def _evidence_of_type(state, *types: str) -> list:
    return [
        e for e in state.get("evidence_index", [])
        if e.get("type") in types
    ]


def _latest_test_results(state, *, item_id: Optional[str] = None, suite: bool = False):
    """Return newest matching test-results evidence entry, or None."""
    matches = []
    for e in state.get("evidence_index", []):
        if e.get("type") != "test-results":
            continue
        meta = e.get("meta") or {}
        if suite:
            if meta.get("scope") == "suite" or not meta.get("item_id"):
                matches.append(e)
        elif item_id:
            if meta.get("item_id") == item_id or item_id in (e.get("item_ids") or []):
                matches.append(e)
        else:
            matches.append(e)
    if not matches:
        return None
    return sorted(matches, key=lambda x: x.get("timestamp", ""))[-1]


def _test_passed(entry: Optional[dict]) -> bool:
    if not entry:
        return False
    meta = entry.get("meta") or {}
    return meta.get("exit_code", 1) == 0


def _item_last_entered_at(project_dir: Path, item_id: str, to_state: str) -> Optional[str]:
    for e in reversed(_load_events(project_dir)):
        if (
            e.get("type") == "item_transition"
            and e.get("item_id") == item_id
            and e.get("to_state") == to_state
        ):
            return e.get("timestamp")
    return None


def _plan_artifact_exists(project_dir: Path, state: dict) -> bool:
    config = _load_config(project_dir)
    artifacts = config.get("artifacts", {})
    ptype = state.get("project_type")
    if ptype == "evolution":
        name = artifacts.get("roadmap", "ROADMAP.md")
    else:
        name = artifacts.get("plan", "BUILD_PLAN.md")
    if (project_dir / name).exists():
        return True
    # Migrated projects may only have evidence pointing at the plan
    return any(e.get("type") == "plan" for e in state.get("evidence_index", []))


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

def _all_items_terminal(state):
    items = state.get("work_items", {})
    if not items:
        return False
    return all(item["state"] in WORK_ITEM_TERMINAL for item in items.values())

def _any_item_accepted(state):
    items = state.get("work_items", {})
    return any(item["state"] == "accepted" for item in items.values())

def _any_item_in_state(state, target_state):
    items = state.get("work_items", {})
    return any(item["state"] == target_state for item in items.values())


def _guard_discover_frame(state):
    ptype = state.get("project_type")
    if ptype == "evolution":
        return bool(_evidence_of_type(state, "health-check", "maturity-assessment"))
    brief = state.get("brief") or {}
    ci = brief.get("core_interaction") or {}
    has_ci = all(ci.get(k) for k in ("description", "input", "output", "hard_part"))
    return has_ci and len(state.get("acceptance_criteria", [])) >= 1


def _guard_frame_plan(state):
    return bool(_evidence_of_type(state, "user-confirmation", "synthesis"))


def _guard_plan_exists(state, project_dir: Optional[Path] = None):
    if len(state.get("work_items", {})) == 0:
        return False
    if project_dir is None:
        return True
    return _plan_artifact_exists(project_dir, state)


def _guard_criteria_mapped(state):
    items = state.get("work_items", {})
    if not items:
        return False
    acs = state.get("acceptance_criteria", [])
    if not acs:
        return False
    return all(item.get("acceptance_criteria") for item in items.values())


def _guard_suite_tests_pass(state):
    return _test_passed(_latest_test_results(state, suite=True))


def _guard_release_stabilize(state):
    return bool(_evidence_of_type(state, "coherence-check", "eval-results"))


def _guard_stabilize_closed(state):
    return bool(_evidence_of_type(state, "user-confirmation")) and _guard_suite_tests_pass(state)


def _deps_met(state, item):
    deps = item.get("depends_on") or []
    items = state.get("work_items", {})
    for dep in deps:
        other = items.get(dep)
        if not other or other["state"] not in ("accepted", "waived"):
            return False
    return True


def _guard_item_has_evidence(item):
    return len(item.get("evidence") or []) >= 1


def _guard_tests_executed(state, item_id, project_dir: Path):
    entry = _latest_test_results(state, item_id=item_id)
    if not entry:
        # Fall back to any suite run after implementing
        entry = _latest_test_results(state, suite=True)
    if not entry:
        return False
    entered = _item_last_entered_at(project_dir, item_id, "implementing")
    if entered and entry.get("timestamp", "") < entered:
        return False
    return _test_passed(entry)


def _guard_regression_pass(state, item_id, project_dir: Path):
    entry = _latest_test_results(state, suite=True)
    if not _test_passed(entry):
        return False
    entered = _item_last_entered_at(project_dir, item_id, "reviewing")
    if entered and entry and entry.get("timestamp", "") < entered:
        return False
    return True


def _apply_guard_results(guards, state, config, project_dir=None, item=None, item_id=None):
    config = config or {}
    overrides = config.get("guard_overrides", {})
    results = {}
    for name, check_fn in guards:
        override = overrides.get(name, "strict")
        if override == "skip":
            results[name] = "not_applicable"
            continue
        try:
            passed = check_fn(state, project_dir, item, item_id)
        except TypeError:
            try:
                passed = check_fn(state)
            except Exception:
                passed = False
        except Exception:
            passed = False

        if passed:
            results[name] = "pass"
        elif override == "warn":
            results[name] = "pass"
        else:
            waivers = state.get("waivers", [])
            if any(w["guard"] == name for w in waivers):
                results[name] = "waived"
            else:
                results[name] = "fail"
    return results


# Guard callables: (state, project_dir, item, item_id) -> bool
TRANSITION_GUARDS = {
    "intake->discover": [
        ("brief_who_populated", lambda s, *_: bool((s.get("brief") or {}).get("who"))),
        ("brief_problem_populated", lambda s, *_: bool((s.get("brief") or {}).get("problem"))),
        ("project_type_determined", lambda s, *_: s.get("project_type") is not None),
    ],
    "discover->frame": [
        ("discover_complete", lambda s, *_: _guard_discover_frame(s)),
    ],
    "frame->plan": [
        ("scope_confirmed", lambda s, *_: _guard_frame_plan(s)),
        ("user_confirmation", lambda s, *_: _guard_frame_plan(s)),
    ],
    "plan->deliver": [
        ("plan_exists", lambda s, p, *_: _guard_plan_exists(s, p)),
        ("criteria_mapped", lambda s, *_: _guard_criteria_mapped(s)),
    ],
    "deliver->release": [
        ("work_items_complete", lambda s, *_: _all_items_terminal(s)),
        ("at_least_one_accepted", lambda s, *_: _any_item_accepted(s)),
        ("no_items_blocked", lambda s, *_: not _any_item_in_state(s, "blocked")),
        ("tests_pass", lambda s, *_: _guard_suite_tests_pass(s)),
    ],
    "release->stabilize": [
        ("coherence_check_passes", lambda s, *_: _guard_release_stabilize(s)),
    ],
    "stabilize->closed": [
        ("user_confirms", lambda s, *_: bool(_evidence_of_type(s, "user-confirmation"))),
        ("final_test_pass", lambda s, *_: _guard_suite_tests_pass(s)),
    ],
}

ITEM_TRANSITION_GUARDS = {
    "ready->implementing": [
        ("dependencies_met", lambda s, _p, item, _i: _deps_met(s, item or {})),
    ],
    "implementing->verifying": [
        ("implementation_evidence", lambda s, _p, item, _i: _guard_item_has_evidence(item or {})),
    ],
    "verifying->reviewing": [
        ("tests_executed", lambda s, p, _item, iid: _guard_tests_executed(s, iid, p)),
    ],
    "reviewing->accepted": [
        ("regression_tests_pass", lambda s, p, _item, iid: _guard_regression_pass(s, iid, p)),
    ],
}


def evaluate_guards(state, target, config=None, project_dir=None):
    key = f"{state['phase']}->{target}"
    guards = TRANSITION_GUARDS.get(key, [])
    return _apply_guard_results(guards, state, config, project_dir=project_dir)


def evaluate_item_guards(state, item_id, target, config=None, project_dir=None):
    items = state.get("work_items", {})
    item = items.get(item_id)
    if not item:
        return {}
    current = item["state"]
    key = f"{current}->{target}"
    guards = ITEM_TRANSITION_GUARDS.get(key, [])
    return _apply_guard_results(
        guards, state, config, project_dir=project_dir, item=item, item_id=item_id
    )


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------

def record_evidence(
    project_dir: Path,
    evidence_type: str,
    *,
    content: Optional[str] = None,
    path: Optional[str] = None,
    item_id: Optional[str] = None,
    meta: Optional[dict] = None,
) -> dict:
    state = _load_state(project_dir)
    _evidence_dir(project_dir).mkdir(parents=True, exist_ok=True)

    key = _next_evidence_key(state)
    ts = _now_iso()
    meta = dict(meta or {})
    if item_id:
        meta.setdefault("item_id", item_id)

    stored_path = None
    file_hash = None

    if path:
        src = Path(path)
        if not src.is_absolute():
            src = project_dir / src
        if not src.exists():
            raise WorkflowError(f"Evidence path does not exist: {src}")
        rel = f".workflow/evidence/{key}-{src.name}"
        dest = project_dir / rel
        dest.write_bytes(src.read_bytes())
        stored_path = rel
        file_hash = _sha256_file(dest)
    elif content is not None:
        rel = f".workflow/evidence/{key}.txt"
        dest = project_dir / rel
        data = content.encode("utf-8")
        dest.write_bytes(data)
        stored_path = rel
        file_hash = _sha256_bytes(data)

    entry = {
        "key": key,
        "type": evidence_type,
        "path": stored_path,
        "hash": file_hash,
        "timestamp": ts,
        "meta": meta,
    }
    if item_id:
        entry["item_ids"] = [item_id]

    state.setdefault("evidence_index", []).append(entry)

    if item_id:
        items = state.get("work_items", {})
        if item_id not in items:
            raise ItemNotFoundError(f"Work item {item_id} not found.")
        items[item_id].setdefault("evidence", []).append(key)

    state["revision"] += 1
    _save_state(state, project_dir)

    event = {
        "event_id": _generate_event_id(),
        "timestamp": ts,
        "type": "evidence",
        "from_state": state["phase"],
        "to_state": state["phase"],
        "item_id": item_id,
        "guard_results": None,
        "evidence_keys": [key],
        "idempotency_key": None,
        "forced": False,
        "reason": f"Recorded evidence {key} ({evidence_type})",
        "actor": "agent",
        "revision": state["revision"],
        "data": {"type": evidence_type, "meta": meta},
    }
    _append_event(event, project_dir)

    return {"key": key, "type": evidence_type, "path": stored_path, "hash": file_hash}


def record_test_results(
    project_dir: Path,
    *,
    command: str,
    exit_code: int,
    stdout_tail: str = "",
    passed: Optional[int] = None,
    failed: Optional[int] = None,
    item_id: Optional[str] = None,
) -> dict:
    """Record a test-results evidence entry. Suite-level when item_id is None."""
    # Auto-attach to the single active implementing/verifying item when possible
    if item_id is None:
        try:
            state = _load_state(project_dir)
            active = [
                wid for wid, item in state.get("work_items", {}).items()
                if item["state"] in ("implementing", "verifying")
            ]
            if len(active) == 1:
                item_id = active[0]
        except WorkflowNotFoundError:
            pass

    scope = "item" if item_id else "suite"
    meta = {
        "command": command,
        "exit_code": exit_code,
        "passed": passed,
        "failed": failed,
        "scope": scope,
    }
    if item_id:
        meta["item_id"] = item_id

    content = (
        f"command: {command}\n"
        f"exit_code: {exit_code}\n"
        f"passed: {passed}\n"
        f"failed: {failed}\n"
        f"scope: {scope}\n"
        f"---\n"
        f"{stdout_tail[-4000:]}"
    )
    return record_evidence(
        project_dir,
        "test-results",
        content=content,
        item_id=item_id,
        meta=meta,
    )


def run_and_record_tests(
    project_dir: Path,
    command: str,
    *,
    item_id: Optional[str] = None,
) -> dict:
    """Run a shell command and record test-results evidence."""
    proc = subprocess.run(
        command,
        shell=True,
        cwd=str(project_dir),
        capture_output=True,
        text=True,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    passed = failed = None
    m = re.search(r"(\d+)\s+passed", out)
    if m:
        passed = int(m.group(1))
    m = re.search(r"(\d+)\s+failed", out)
    if m:
        failed = int(m.group(1))

    result = record_test_results(
        project_dir,
        command=command,
        exit_code=proc.returncode,
        stdout_tail=out,
        passed=passed,
        failed=failed,
        item_id=item_id,
    )
    result["exit_code"] = proc.returncode
    result["passed"] = passed
    result["failed"] = failed
    return result


def active_item_for_tests(project_dir: Path) -> Optional[str]:
    state = _load_state(project_dir)
    active = [
        wid for wid, item in state.get("work_items", {}).items()
        if item["state"] in ("implementing", "verifying")
    ]
    if len(active) == 1:
        return active[0]
    return None


# ---------------------------------------------------------------------------
# Migration parsers
# ---------------------------------------------------------------------------

def _parse_build_plan(content: str, state: dict) -> dict:
    """Parse BUILD_PLAN.md steps into work items."""
    step_re = re.compile(
        r"^###\s+Step\s+(\d+)\s*:\s*(.+)$",
        re.MULTILINE | re.IGNORECASE,
    )
    matches = list(step_re.finditer(content))
    items_migrated = 0
    items_completed = 0

    for i, match in enumerate(matches):
        name = match.group(2).strip()
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        block = content[start:end]

        status_m = re.search(r"(?i)\*\*Status\*\*\s*:\s*([^\n]+)", block)
        status_text = (status_m.group(1).strip().lower() if status_m else "not started")

        if "complete" in status_text or "done" in status_text or "accepted" in status_text:
            wi_state = "accepted"
            items_completed += 1
        elif "progress" in status_text or "implement" in status_text:
            wi_state = "implementing"
        else:
            wi_state = "ready"

        ac_id = _next_ac_id(state)
        state.setdefault("acceptance_criteria", []).append({
            "id": ac_id,
            "criterion": f"Complete step: {name}",
            "testable": True,
            "status": "met" if wi_state == "accepted" else "pending",
        })

        item_id = _next_item_id(state)
        state.setdefault("work_items", {})[item_id] = {
            "name": name,
            "state": wi_state,
            "depends_on": [],
            "acceptance_criteria": [ac_id],
            "evidence": [],
            "waive_reason": None,
            "waive_approver": None,
        }
        items_migrated += 1

    return {"items_migrated": items_migrated, "items_completed": items_completed}


def _parse_roadmap(content: str, state: dict) -> dict:
    """Parse ROADMAP.md NOW bucket into work items; note NEXT/LATER."""
    items_migrated = 0
    items_completed = 0
    deferred = {"NEXT": [], "LATER": [], "PARKED": []}

    # Split by ## headings
    sections = re.split(r"^##\s+", content, flags=re.MULTILINE)
    for section in sections[1:]:
        lines = section.strip().splitlines()
        if not lines:
            continue
        heading = lines[0].strip().upper()
        body = "\n".join(lines[1:])
        bullets = re.findall(r"^[-*]\s+(.+)$", body, re.MULTILINE)

        if heading.startswith("NOW"):
            for bullet in bullets:
                name = bullet.strip()
                # Detect in-progress markers
                lower = name.lower()
                if "(done)" in lower or "[x]" in lower:
                    wi_state = "accepted"
                    items_completed += 1
                    name = re.sub(r"\(done\)|\[x\]|\[X\]", "", name, flags=re.I).strip()
                elif "(in progress)" in lower or "[~]" in lower:
                    wi_state = "implementing"
                    name = re.sub(
                        r"\(in progress\)|\[~\]", "", name, flags=re.I
                    ).strip()
                else:
                    wi_state = "ready"

                ac_id = _next_ac_id(state)
                state.setdefault("acceptance_criteria", []).append({
                    "id": ac_id,
                    "criterion": f"Deliver: {name}",
                    "testable": True,
                    "status": "met" if wi_state == "accepted" else "pending",
                })
                item_id = _next_item_id(state)
                state.setdefault("work_items", {})[item_id] = {
                    "name": name,
                    "state": wi_state,
                    "depends_on": [],
                    "acceptance_criteria": [ac_id],
                    "evidence": [],
                    "waive_reason": None,
                    "waive_approver": None,
                }
                items_migrated += 1
        elif heading.startswith("NEXT"):
            deferred["NEXT"] = [b.strip() for b in bullets]
        elif heading.startswith("LATER"):
            deferred["LATER"] = [b.strip() for b in bullets]
        elif heading.startswith("PARKED"):
            deferred["PARKED"] = [b.strip() for b in bullets]

    state["deferred_plan"] = deferred
    return {"items_migrated": items_migrated, "items_completed": items_completed, "deferred": deferred}


def _seed_plan_evidence(state, project_dir: Path, filename: str) -> str:
    """Add a plan evidence entry pointing at a legacy artifact without copying."""
    key = _next_evidence_key(state)
    path = project_dir / filename
    file_hash = _sha256_file(path) if path.exists() else None
    entry = {
        "key": key,
        "type": "plan",
        "path": filename,
        "hash": file_hash,
        "timestamp": _now_iso(),
        "meta": {"migrated": True, "source": filename},
    }
    state.setdefault("evidence_index", []).append(entry)
    return key


def _handle_migration(state, project_dir: Path):
    build_plan = project_dir / "BUILD_PLAN.md"
    roadmap = project_dir / "ROADMAP.md"
    stats = {"items_migrated": 0, "items_completed": 0}

    if roadmap.exists():
        state["migrated_from"] = "product-evolution"
        state["project_type"] = "evolution"
        content = roadmap.read_text()
        stats = _parse_roadmap(content, state)
        _seed_plan_evidence(state, project_dir, "ROADMAP.md")
        # Infer phase
        if stats["items_migrated"] == 0:
            state["phase"] = "plan"
        elif stats["items_completed"] == stats["items_migrated"] and stats["items_migrated"] > 0:
            state["phase"] = "release"
        else:
            state["phase"] = "deliver"
        state["status"] = "active"
    elif build_plan.exists():
        state["migrated_from"] = "product-discovery"
        state["project_type"] = "greenfield"
        content = build_plan.read_text()
        stats = _parse_build_plan(content, state)
        _seed_plan_evidence(state, project_dir, "BUILD_PLAN.md")
        if stats["items_migrated"] == 0:
            state["phase"] = "plan"
        elif stats["items_completed"] == stats["items_migrated"]:
            state["phase"] = "release"
        elif stats["items_completed"] > 0:
            state["phase"] = "deliver"
        else:
            state["phase"] = "plan"
        state["status"] = "active"

    state["_migration_stats"] = stats
    return state


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------

def init_workflow(
    project_dir: Path,
    project_type: Optional[str] = None,
    from_migration: bool = False,
) -> dict:
    wf = _wf_path(project_dir)
    if (wf / STATE_FILE).exists():
        raise WorkflowExistsError("Workflow already exists. Use status to inspect.")

    wf.mkdir(parents=True, exist_ok=True)
    (wf / EVIDENCE_DIR).mkdir(exist_ok=True)

    wf_id = _generate_workflow_id()
    ts = _now_iso()

    state = {
        "schema_version": SCHEMA_VERSION,
        "workflow_id": wf_id,
        "revision": 0,
        "phase": "intake",
        "status": "active",
        "resume_state": None,
        "project_type": project_type,
        "objective": None,
        "scope": {"included": [], "excluded": []},
        "brief": {},
        "acceptance_criteria": [],
        "work_items": {},
        "risks": [],
        "decisions": [],
        "waivers": [],
        "evidence_index": [],
        "last_transition": None,
        "blocked_reason": None,
        "blocked_owner": None,
        "abort_reason": None,
        "created_at": ts,
        "updated_at": ts,
        "migrated_from": None,
    }

    migration_stats = None
    if from_migration:
        state = _handle_migration(state, project_dir)
        migration_stats = state.pop("_migration_stats", None)

    _save_state(state, project_dir)

    event = {
        "event_id": _generate_event_id(),
        "timestamp": ts,
        "type": "migrate" if state.get("migrated_from") else "init",
        "from_state": None,
        "to_state": state["phase"],
        "item_id": None,
        "guard_results": None,
        "evidence_keys": [e["key"] for e in state.get("evidence_index", [])],
        "idempotency_key": None,
        "forced": False,
        "reason": f"Workflow initialized. Type: {state['project_type'] or 'undetermined'}.",
        "actor": "system",
        "revision": 0,
        "data": {
            "migrated_from": state.get("migrated_from"),
            "migration_stats": migration_stats,
        },
    }
    _append_event(event, project_dir)

    result = {
        "workflow_id": wf_id,
        "phase": state["phase"],
        "project_type": state["project_type"],
        "migrated_from": state.get("migrated_from"),
    }
    if migration_stats:
        result["migration_stats"] = migration_stats
        result["work_items"] = len(state.get("work_items", {}))
    return result


def get_status(project_dir: Path) -> dict:
    state = _load_state(project_dir)
    events = _load_events(project_dir)
    return {
        "workflow_id": state["workflow_id"],
        "phase": state["phase"],
        "status": state["status"],
        "project_type": state["project_type"],
        "revision": state["revision"],
        "blocked_reason": state.get("blocked_reason"),
        "blocked_owner": state.get("blocked_owner"),
        "resume_state": state.get("resume_state"),
        "work_items": {
            wid: {"name": item["name"], "state": item["state"]}
            for wid, item in state.get("work_items", {}).items()
        },
        "work_items_summary": _work_items_summary(state),
        "evidence_count": len(state.get("evidence_index", [])),
        "recent_events": [
            {
                "event_id": e["event_id"],
                "timestamp": e["timestamp"],
                "type": e["type"],
                "reason": e.get("reason"),
            }
            for e in events[-5:]
        ],
    }


def get_next_transitions(project_dir: Path) -> dict:
    state = _load_state(project_dir)
    config = _load_config(project_dir)
    phase = state["phase"]

    if phase in TERMINAL_STATES:
        return {
            "current": phase,
            "allowed": [],
            "message": f"Workflow is {phase}. No transitions available.",
        }

    if phase == "blocked":
        return {
            "current": "blocked",
            "resume_state": state.get("resume_state"),
            "allowed": [{"target": state.get("resume_state"), "action": "resume"}],
            "message": (
                f"Blocked: {state.get('blocked_reason')}. "
                f"Resume to return to {state.get('resume_state')}."
            ),
        }

    targets = VALID_TRANSITIONS.get(phase, [])
    allowed = []
    for target in targets:
        if target in ("blocked", "aborted"):
            allowed.append({"target": target, "guards": []})
            continue
        guard_results = evaluate_guards(state, target, config, project_dir=project_dir)
        guards = [{"name": n, "status": s} for n, s in guard_results.items()]
        allowed.append({"target": target, "guards": guards})

    return {"current": phase, "allowed": allowed}


def do_transition(
    project_dir: Path,
    target: str,
    evidence: Optional[list] = None,
    force: bool = False,
    reason: Optional[str] = None,
) -> dict:
    state = _load_state(project_dir)
    config = _load_config(project_dir)
    phase = state["phase"]

    if phase in TERMINAL_STATES:
        raise InvalidPhaseError(f"Workflow is {phase}. No transitions allowed.")
    if phase == "blocked":
        raise InvalidPhaseError("Workflow is blocked. Use resume to unblock.")

    valid = VALID_TRANSITIONS.get(phase, [])
    if target not in valid:
        raise InvalidTransitionError(
            f"Cannot transition from {phase} to {target}. Valid targets: {', '.join(valid)}"
        )

    idem_key = _make_idempotency_key(state, target)
    if _check_idempotency(idem_key, project_dir):
        return {
            "transition": f"{phase} → {target}",
            "idempotent": True,
            "message": "Transition already recorded. No change.",
        }

    guard_results = evaluate_guards(state, target, config, project_dir=project_dir)
    failed = {k: v for k, v in guard_results.items() if v == "fail"}

    if failed and not force:
        raise GuardFailedError(
            f"Guards blocked transition {phase} → {target}: {', '.join(failed.keys())}",
            guard_results=guard_results,
        )

    state["revision"] += 1
    state["phase"] = target
    state["status"] = _derive_status(target)

    event = {
        "event_id": _generate_event_id(),
        "timestamp": _now_iso(),
        "type": "transition",
        "from_state": phase,
        "to_state": target,
        "item_id": None,
        "guard_results": guard_results,
        "evidence_keys": evidence or [],
        "idempotency_key": idem_key,
        "forced": force and bool(failed),
        "reason": reason,
        "actor": "agent",
        "revision": state["revision"],
        "data": None,
    }

    state["last_transition"] = event["event_id"]
    _save_state(state, project_dir)
    _append_event(event, project_dir)

    return {
        "transition": f"{phase} → {target}",
        "revision": state["revision"],
        "forced": event["forced"],
        "guard_results": guard_results,
    }


def add_item(
    project_dir: Path,
    name: str,
    acceptance_criteria: Optional[list] = None,
    risks: Optional[list] = None,
) -> dict:
    state = _load_state(project_dir)
    if state["phase"] not in ("plan", "deliver"):
        raise InvalidPhaseError(
            f"Can only add items in plan or deliver phase (current: {state['phase']})."
        )

    item_id = _next_item_id(state)
    ac_refs = acceptance_criteria or []
    risk_refs = risks or []

    state["work_items"][item_id] = {
        "name": name,
        "state": "ready",
        "depends_on": [],
        "acceptance_criteria": ac_refs,
        "evidence": [],
        "waive_reason": None,
        "waive_approver": None,
    }
    state["revision"] += 1
    _save_state(state, project_dir)

    event = {
        "event_id": _generate_event_id(),
        "timestamp": _now_iso(),
        "type": "item_add",
        "from_state": None,
        "to_state": "ready",
        "item_id": item_id,
        "guard_results": None,
        "evidence_keys": [],
        "idempotency_key": None,
        "forced": False,
        "reason": f"Added work item: {name}",
        "actor": "agent",
        "revision": state["revision"],
        "data": {"ac": ac_refs, "risks": risk_refs},
    }
    _append_event(event, project_dir)

    return {"item_id": item_id, "name": name, "state": "ready"}


def list_items(
    project_dir: Path,
    state_filter: Optional[str] = None,
) -> dict:
    state = _load_state(project_dir)
    items = state.get("work_items", {})
    result = []
    for wid, item in items.items():
        if state_filter and item["state"] != state_filter:
            continue
        result.append({
            "id": wid,
            "name": item["name"],
            "state": item["state"],
            "acceptance_criteria": item.get("acceptance_criteria", []),
            "evidence": item.get("evidence", []),
        })
    return {"items": result, "summary": _work_items_summary(state)}


def transition_item(
    project_dir: Path,
    item_id: str,
    target: str,
    reason: Optional[str] = None,
    force: bool = False,
) -> dict:
    state = _load_state(project_dir)
    config = _load_config(project_dir)
    if state["phase"] != "deliver":
        raise InvalidPhaseError(
            f"Item transitions only valid in deliver phase (current: {state['phase']})."
        )

    items = state.get("work_items", {})
    if item_id not in items:
        raise ItemNotFoundError(f"Work item {item_id} not found.")

    item = items[item_id]
    current = item["state"]
    valid = VALID_ITEM_TRANSITIONS.get(current, [])

    if target != "cancelled" and target not in valid:
        raise InvalidTransitionError(
            f"Cannot transition {item_id} from {current} to {target}. Valid: {', '.join(valid)}"
        )

    idem_key = _make_idempotency_key(state, target, item_id)
    if _check_idempotency(idem_key, project_dir):
        return {
            "item_id": item_id,
            "transition": f"{current} → {target}",
            "idempotent": True,
        }

    guard_results = {}
    if target != "cancelled":
        guard_results = evaluate_item_guards(
            state, item_id, target, config, project_dir=project_dir
        )
        failed = {k: v for k, v in guard_results.items() if v == "fail"}
        if failed and not force:
            raise GuardFailedError(
                f"Guards blocked item {item_id} {current} → {target}: "
                f"{', '.join(failed.keys())}",
                guard_results=guard_results,
            )

    item["state"] = target
    state["revision"] += 1
    _save_state(state, project_dir)

    event = {
        "event_id": _generate_event_id(),
        "timestamp": _now_iso(),
        "type": "item_transition",
        "from_state": current,
        "to_state": target,
        "item_id": item_id,
        "guard_results": guard_results or None,
        "evidence_keys": list(item.get("evidence") or []),
        "idempotency_key": idem_key,
        "forced": force and bool(guard_results),
        "reason": reason,
        "actor": "agent",
        "revision": state["revision"],
        "data": None,
    }
    _append_event(event, project_dir)

    return {
        "item_id": item_id,
        "transition": f"{current} → {target}",
        "revision": state["revision"],
        "guard_results": guard_results,
        "forced": event["forced"],
    }


def waive_item(
    project_dir: Path,
    item_id: str,
    approver: str,
    reason: str,
) -> dict:
    state = _load_state(project_dir)
    items = state.get("work_items", {})
    if item_id not in items:
        raise ItemNotFoundError(f"Work item {item_id} not found.")

    item = items[item_id]
    if item["state"] != "reviewing":
        raise InvalidTransitionError(
            f"Can only waive items in reviewing state (current: {item['state']})."
        )

    item["state"] = "waived"
    item["waive_approver"] = approver
    item["waive_reason"] = reason
    state["revision"] += 1
    _save_state(state, project_dir)

    event = {
        "event_id": _generate_event_id(),
        "timestamp": _now_iso(),
        "type": "waive",
        "from_state": "reviewing",
        "to_state": "waived",
        "item_id": item_id,
        "guard_results": None,
        "evidence_keys": [],
        "idempotency_key": None,
        "forced": False,
        "reason": f"Waived by {approver}: {reason}",
        "actor": "user",
        "revision": state["revision"],
        "data": {"approver": approver, "reason": reason},
    }
    _append_event(event, project_dir)

    return {"item_id": item_id, "state": "waived", "approver": approver}


def check_guards(project_dir: Path) -> dict:
    state = _load_state(project_dir)
    config = _load_config(project_dir)
    phase = state["phase"]

    if phase in TERMINAL_STATES:
        return {"phase": phase, "terminal": True, "transitions": {}}
    if phase == "blocked":
        return {"phase": phase, "blocked": True, "transitions": {}}

    targets = [
        t for t in VALID_TRANSITIONS.get(phase, []) if t not in ("blocked", "aborted")
    ]
    transitions = {}
    for target in targets:
        guard_results = evaluate_guards(state, target, config, project_dir=project_dir)
        all_pass = all(
            v in ("pass", "waived", "not_applicable") for v in guard_results.values()
        )
        transitions[f"{phase}->{target}"] = {
            "ready": all_pass,
            "guards": guard_results,
        }

    return {"phase": phase, "transitions": transitions}


def check_ci(project_dir: Path) -> dict:
    """CI-oriented check: exit-ready verdict for merge gates."""
    try:
        state = _load_state(project_dir)
    except WorkflowNotFoundError:
        return {
            "ok": True,
            "skipped": True,
            "message": "No workflow — CI check skipped.",
        }

    phase = state["phase"]
    if phase in TERMINAL_STATES:
        return {"ok": True, "phase": phase, "message": f"Workflow is {phase}."}

    if phase == "blocked":
        return {
            "ok": False,
            "phase": phase,
            "message": f"Workflow blocked: {state.get('blocked_reason')}",
        }

    problems = []
    guard_check = check_guards(project_dir)

    if phase in ("deliver", "release"):
        if not _guard_suite_tests_pass(state):
            problems.append("No recent passing suite test-results evidence.")

    # Next forward transition readiness (informational + fail if deliver/release)
    for key, info in guard_check.get("transitions", {}).items():
        if not info["ready"] and phase in ("deliver", "release", "stabilize"):
            failed = [n for n, s in info["guards"].items() if s == "fail"]
            if failed:
                problems.append(f"{key} blocked by: {', '.join(failed)}")

    return {
        "ok": len(problems) == 0,
        "phase": phase,
        "problems": problems,
        "transitions": guard_check.get("transitions", {}),
        "message": "OK" if not problems else "; ".join(problems),
    }


def preview_item_transition(
    project_dir: Path,
    item_id: str,
    target: str,
) -> dict:
    """Dry-run item guards without mutating state (for hooks)."""
    state = _load_state(project_dir)
    config = _load_config(project_dir)
    if item_id not in state.get("work_items", {}):
        raise ItemNotFoundError(f"Work item {item_id} not found.")
    results = evaluate_item_guards(
        state, item_id, target, config, project_dir=project_dir
    )
    failed = {k: v for k, v in results.items() if v == "fail"}
    return {
        "item_id": item_id,
        "target": target,
        "allowed": len(failed) == 0,
        "guard_results": results,
        "failed": list(failed.keys()),
    }


def preview_transition(project_dir: Path, target: str) -> dict:
    state = _load_state(project_dir)
    config = _load_config(project_dir)
    results = evaluate_guards(state, target, config, project_dir=project_dir)
    failed = {k: v for k, v in results.items() if v == "fail"}
    return {
        "from_phase": state["phase"],
        "target": target,
        "allowed": len(failed) == 0,
        "guard_results": results,
        "failed": list(failed.keys()),
    }


def block_workflow(
    project_dir: Path,
    reason: str,
    owner: str,
) -> dict:
    state = _load_state(project_dir)
    phase = state["phase"]

    if phase in TERMINAL_STATES:
        raise InvalidPhaseError(f"Cannot block a {phase} workflow.")
    if phase == "blocked":
        raise InvalidPhaseError("Workflow is already blocked.")

    state["resume_state"] = phase
    state["phase"] = "blocked"
    state["status"] = "blocked"
    state["blocked_reason"] = reason
    state["blocked_owner"] = owner
    state["revision"] += 1

    event = {
        "event_id": _generate_event_id(),
        "timestamp": _now_iso(),
        "type": "block",
        "from_state": phase,
        "to_state": "blocked",
        "item_id": None,
        "guard_results": None,
        "evidence_keys": [],
        "idempotency_key": None,
        "forced": False,
        "reason": reason,
        "actor": "agent",
        "revision": state["revision"],
        "data": {"owner": owner},
    }

    state["last_transition"] = event["event_id"]
    _save_state(state, project_dir)
    _append_event(event, project_dir)

    return {"transition": f"{phase} → blocked", "reason": reason, "owner": owner}


def resume_workflow(
    project_dir: Path,
    reason: Optional[str] = None,
) -> dict:
    state = _load_state(project_dir)
    if state["phase"] != "blocked":
        raise InvalidPhaseError("Workflow is not blocked.")

    resume_to = state["resume_state"]
    if not resume_to:
        raise WorkflowError("No resume state recorded.")

    state["phase"] = resume_to
    state["status"] = "active"
    state["resume_state"] = None
    state["blocked_reason"] = None
    state["blocked_owner"] = None
    state["revision"] += 1

    event = {
        "event_id": _generate_event_id(),
        "timestamp": _now_iso(),
        "type": "resume",
        "from_state": "blocked",
        "to_state": resume_to,
        "item_id": None,
        "guard_results": None,
        "evidence_keys": [],
        "idempotency_key": None,
        "forced": False,
        "reason": reason,
        "actor": "agent",
        "revision": state["revision"],
        "data": None,
    }

    state["last_transition"] = event["event_id"]
    _save_state(state, project_dir)
    _append_event(event, project_dir)

    return {"transition": f"blocked → {resume_to}", "revision": state["revision"]}


def waive_guard(
    project_dir: Path,
    guard: str,
    approver: str,
    reason: str,
) -> dict:
    state = _load_state(project_dir)

    waiver = {
        "guard": guard,
        "approver": approver,
        "reason": reason,
        "scope": state["phase"],
        "risk_accepted": f"Guard {guard} waived",
        "timestamp": _now_iso(),
    }
    state.setdefault("waivers", []).append(waiver)
    state["revision"] += 1
    _save_state(state, project_dir)

    event = {
        "event_id": _generate_event_id(),
        "timestamp": _now_iso(),
        "type": "waive",
        "from_state": state["phase"],
        "to_state": state["phase"],
        "item_id": None,
        "guard_results": None,
        "evidence_keys": [],
        "idempotency_key": None,
        "forced": False,
        "reason": f"Guard '{guard}' waived by {approver}: {reason}",
        "actor": "user",
        "revision": state["revision"],
        "data": {"guard": guard, "approver": approver},
    }
    _append_event(event, project_dir)

    return {"guard": guard, "approver": approver, "phase": state["phase"]}


def close_workflow(
    project_dir: Path,
    reason: Optional[str] = None,
    force: bool = False,
) -> dict:
    state = _load_state(project_dir)
    phase = state["phase"]

    if phase == "closed":
        return {"phase": "closed", "message": "Workflow is already closed."}

    if phase != "stabilize" and not force:
        raise InvalidPhaseError(
            f"Can only close from stabilize phase (current: {phase}). Use force to override."
        )

    prev_rev = state["revision"]
    state["phase"] = "closed"
    state["status"] = "terminal"
    state["revision"] += 1

    idem_key = _make_idempotency_key(
        {"workflow_id": state["workflow_id"], "revision": prev_rev, "phase": phase},
        "closed",
    )

    event = {
        "event_id": _generate_event_id(),
        "timestamp": _now_iso(),
        "type": "transition",
        "from_state": phase,
        "to_state": "closed",
        "item_id": None,
        "guard_results": None,
        "evidence_keys": [],
        "idempotency_key": idem_key,
        "forced": phase != "stabilize",
        "reason": reason or "Workflow closed.",
        "actor": "user",
        "revision": state["revision"],
        "data": None,
    }

    state["last_transition"] = event["event_id"]
    _save_state(state, project_dir)
    _append_event(event, project_dir)

    return {
        "transition": f"{phase} → closed",
        "revision": state["revision"],
        "forced": phase != "stabilize",
    }


def render_status(project_dir: Path) -> str:
    state = _load_state(project_dir)
    events = _load_events(project_dir)

    lines = [
        f"# Workflow Status: {state['workflow_id']}",
        "",
        f"**Phase**: {state['phase']}  ",
        f"**Status**: {state['status']}  ",
        f"**Type**: {state['project_type'] or 'undetermined'}  ",
        f"**Revision**: {state['revision']}  ",
        "",
    ]

    if state.get("objective"):
        lines.append(f"**Objective**: {state['objective']}")
        lines.append("")

    if state["phase"] == "blocked":
        lines.extend([
            f"**Blocked**: {state.get('blocked_reason', '?')}  ",
            f"**Owner**: {state.get('blocked_owner', '?')}  ",
            f"**Resume to**: {state.get('resume_state', '?')}  ",
            "",
        ])

    items = state.get("work_items", {})
    if items:
        summary = _work_items_summary(state)
        lines.append(f"## Work Items ({summary['total']})")
        lines.append("")
        lines.append(
            f"Progress: {summary['accepted']} accepted, {summary['in_progress']} in progress, "
            f"{summary['blocked']} blocked, {summary['ready']} ready"
        )
        lines.append("")
        lines.append("| ID | Name | State |")
        lines.append("|---|---|---|")
        for wid, item in items.items():
            lines.append(f"| {wid} | {item['name']} | {item['state']} |")
        lines.append("")

    evidence = state.get("evidence_index", [])
    if evidence:
        lines.append(f"## Evidence ({len(evidence)})")
        lines.append("")
        for e in evidence[-10:]:
            lines.append(f"- {e['key']}: {e['type']} ({e.get('timestamp', '')[:19]})")
        lines.append("")

    recent = events[-5:] if events else []
    if recent:
        lines.append("## Recent Events")
        lines.append("")
        for e in recent:
            lines.append(f"- [{e['timestamp'][:19]}] {e['type']}: {e.get('reason', '-')}")
        lines.append("")

    return "\n".join(lines)
