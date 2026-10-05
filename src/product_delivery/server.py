"""
Product Delivery MCP Server

Exposes the delivery workflow state machine as MCP tools, resources,
and prompts. Any harness that speaks MCP gets identical behavior.

Multi-project: every tool accepts an optional project_dir parameter.
When omitted, the server's startup directory is used.

Configuration via .env in the project directory (or PRODUCT_DELIVERY_*
environment variables):

    PRODUCT_DELIVERY_PROJECT_TYPE=greenfield|evolution
    PRODUCT_DELIVERY_GUARD_MODE=strict|warn|skip
    PRODUCT_DELIVERY_SKIP_STATES=discover,frame
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from mcp.server.fastmcp import FastMCP as MCPServer
except (ImportError, ModuleNotFoundError, TypeError):
    try:
        from mcp.server.mcpserver import MCPServer
    except (ImportError, ModuleNotFoundError):
        from mcp.server.fastmcp import FastMCP as MCPServer

# mcp 1.x accepts version=; mcp 2.x MCPServer may not
try:
    mcp = MCPServer(
        "product-delivery",
        version="1.0.0",
        instructions=(
            "Unified lifecycle server for software products. Manages a "
            "hierarchical state machine from intake through delivery and "
            "stabilization, with a nested work-item submachine. "
            "FIRST STEP: always call workflow_detect — it returns a typed path "
            "and ordered next_actions. Execute those actions in order "
            "(setup → init/migrate → status). Do not skip setup on a bare repo. "
            "Evidence is machine-checked: record test runs and artifacts before "
            "advancing work items or phases. "
            "All tools accept an optional project_dir. "
            "Read the product_delivery prompt for full skill instructions."
        ),
    )
except TypeError:
    mcp = MCPServer(
        "product-delivery",
        instructions=(
            "Unified lifecycle server for software products. Manages a "
            "hierarchical state machine from intake through delivery and "
            "stabilization, with a nested work-item submachine. "
            "FIRST STEP: always call workflow_detect — it returns a typed path "
            "and ordered next_actions. Execute those actions in order "
            "(setup → init/migrate → status). Do not skip setup on a bare repo. "
            "Evidence is machine-checked: record test runs and artifacts before "
            "advancing work items or phases. "
            "All tools accept an optional project_dir. "
            "Read the product_delivery prompt for full skill instructions."
        ),
    )

from . import engine

# Default project dir, set at startup. Tools use this when project_dir is omitted.
DEFAULT_PROJECT_DIR = Path.cwd()

# Locate the package's static files (references/, templates/)
PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent

# Config cache per project dir
_config_cache: dict[str, dict[str, str]] = {}

# Project registry — central record of all known projects
REGISTRY_DIR = Path.home() / ".product-delivery"
REGISTRY_PATH = REGISTRY_DIR / "projects.json"


def _load_registry() -> dict:
    if REGISTRY_PATH.exists():
        try:
            return json.loads(REGISTRY_PATH.read_text())
        except (json.JSONDecodeError, ValueError):
            return {"projects": {}}
    return {"projects": {}}


def _save_registry(registry: dict):
    try:
        REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
        REGISTRY_PATH.write_text(json.dumps(registry, indent=2) + "\n")
    except OSError:
        # Best-effort — tests / restricted environments may block home writes
        pass


def _register_project(project_dir: Path, phase: str | None = None):
    try:
        registry = _load_registry()
    except OSError:
        return
    key = str(project_dir)
    now = datetime.now(timezone.utc).isoformat()
    if key in registry["projects"]:
        registry["projects"][key]["last_seen"] = now
        if phase is not None:
            registry["projects"][key]["phase"] = phase
    else:
        registry["projects"][key] = {
            "name": project_dir.name,
            "registered_at": now,
            "last_seen": now,
            "phase": phase,
        }
    _save_registry(registry)


def _resolve(project_dir: str | None) -> Path:
    if project_dir:
        return Path(project_dir).resolve()
    return DEFAULT_PROJECT_DIR


def _load_env(project_dir: Path) -> dict[str, str]:
    cache_key = str(project_dir)
    if cache_key in _config_cache:
        return _config_cache[cache_key]

    config: dict[str, str] = {}
    env_file = project_dir / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip("\"'")
                if key.startswith("PRODUCT_DELIVERY_"):
                    config[key] = value

    for key, value in os.environ.items():
        if key.startswith("PRODUCT_DELIVERY_"):
            config[key] = value

    _config_cache[cache_key] = config
    return config


def _get_config(project_dir: Path, key: str, default: str = "") -> str:
    config = _load_env(project_dir)
    return config.get(f"PRODUCT_DELIVERY_{key}", default)


def _error_response(e: Exception) -> str:
    return json.dumps({"error": type(e).__name__, "message": str(e)})


# ---------------------------------------------------------------------------
# Auto-detection
# ---------------------------------------------------------------------------

@mcp.tool()
def workflow_detect(project_dir: str | None = None) -> str:
    """Detect project state and return an ordered next_actions playbook.

    Call this FIRST when entering a project. Follow next_actions in order.

    Paths: resume | legacy_migration | established_evolution | greenfield

    Args:
        project_dir: Project directory to check. Defaults to working directory.
    """
    from .classify import classify_project

    p = _resolve(project_dir)
    result = classify_project(p)
    phase = None

    if result["path"] == "resume":
        try:
            status = engine.get_status(p)
            phase = status.get("phase", "unknown")
            result["phase"] = phase
            result["state"] = "active_workflow"
            result["action"] = (
                f"Workflow is in '{phase}' phase. Call workflow_status for details."
            )
        except engine.WorkflowError:
            result["phase"] = "error"
            result["state"] = "active_workflow"
            result["action"] = (
                "Workflow state file exists but could not be read. "
                "Check .workflow/state.json."
            )

    _register_project(p, phase or result.get("phase"))

    return json.dumps({
        "project_dir": str(p),
        "project_name": p.name,
        "path": result["path"],
        "state": result["state"],
        "phase": result.get("phase"),
        "action": result["action"],
        "next_actions": result["next_actions"],
        "details": result["details"],
    }, indent=2)


@mcp.tool()
def workflow_bootstrap(project_dir: str | None = None, dry_run: bool = False) -> str:
    """Run the detect playbook: setup (if needed) then init/migrate.

    For brand-new, established, or legacy (ROADMAP/BUILD_PLAN) repos.
    Pass dry_run=True to preview without writing.

    Args:
        project_dir: Project directory. Defaults to working directory.
        dry_run: If True, return the playbook without applying setup/init.
    """
    from .classify import classify_project
    from . import setup as setup_mod

    p = _resolve(project_dir)
    classified = classify_project(p)

    if dry_run:
        return json.dumps({
            "project_dir": str(p),
            "dry_run": True,
            "path": classified["path"],
            "next_actions": classified["next_actions"],
            "message": "Dry run — call again with dry_run=False to apply.",
        }, indent=2)

    applied = []
    if classified["path"] == "resume":
        status = engine.get_status(p)
        return json.dumps({
            "project_dir": str(p),
            "path": "resume",
            "phase": status.get("phase"),
            "applied": [],
            "next_actions": classified["next_actions"],
            "message": "Workflow already active — resume from status.",
        }, indent=2)

    needs_setup = classified["state"] == "not_setup"
    if needs_setup:
        setup_result = setup_mod.execute_setup(p)
        applied.append({"step": "setup", "result": setup_result})

    init_args = {}
    for action in classified["next_actions"]:
        if action.get("tool") == "workflow_init":
            init_args = action.get("args") or {}
            break

    if (p / ".workflow" / "state.json").exists():
        status = engine.get_status(p)
        return json.dumps({
            "project_dir": str(p),
            "path": classified["path"],
            "phase": status.get("phase"),
            "applied": applied,
            "message": "Setup applied; workflow already existed.",
            "next_actions": [
                {"tool": "workflow_status", "args": {}},
                {"instruction": "Continue from current phase."},
            ],
        }, indent=2)

    init_result = engine.init_workflow(
        p,
        project_type=init_args.get("project_type"),
        from_migration=bool(init_args.get("from_migration")),
    )
    applied.append({"step": "init", "result": init_result})
    _register_project(p, init_result.get("phase"))

    remaining = [
        a for a in classified["next_actions"]
        if a.get("tool") not in ("workflow_setup", "workflow_init")
    ]

    return json.dumps({
        "project_dir": str(p),
        "path": classified["path"],
        "phase": init_result.get("phase"),
        "project_type": init_result.get("project_type"),
        "migrated_from": init_result.get("migrated_from"),
        "applied": applied,
        "next_actions": remaining,
        "message": "Bootstrap complete. Follow remaining next_actions.",
    }, indent=2)


@mcp.tool()
def workflow_projects(refresh: bool = False) -> str:
    """List all known projects managed by this server.

    Shows every project that has been detected, initialized, or set up,
    along with its current phase and last-seen timestamp.

    Args:
        refresh: If True, re-check each project's live state from disk.
                 If False, return cached registry data (faster).
    """
    registry = _load_registry()
    projects = registry.get("projects", {})

    if not projects:
        return json.dumps({
            "count": 0,
            "projects": [],
            "message": "No projects registered yet. Call workflow_detect on a project to register it.",
        }, indent=2)

    result_list = []
    for path_str, info in projects.items():
        entry = {
            "project_dir": path_str,
            "name": info.get("name", Path(path_str).name),
            "registered_at": info.get("registered_at"),
            "last_seen": info.get("last_seen"),
            "phase": info.get("phase"),
        }

        if refresh:
            p = Path(path_str)
            if not p.exists():
                entry["status"] = "directory_missing"
                entry["phase"] = None
            elif (p / ".workflow" / "state.json").exists():
                try:
                    status = engine.get_status(p)
                    entry["phase"] = status.get("phase", "unknown")
                    entry["status"] = "active"
                    entry["work_items_summary"] = status.get("work_items_summary")
                except engine.WorkflowError:
                    entry["status"] = "error"
            elif any(
                "product-delivery" in _read_json_safe(p / sub / f).get("mcpServers", {})
                for sub, f in [(".claude", "settings.json"), (".cursor", "mcp.json")]
            ):
                entry["status"] = "setup_no_workflow"
                entry["phase"] = None
            else:
                entry["status"] = "not_setup"
                entry["phase"] = None

            _register_project(p, entry.get("phase"))

        result_list.append(entry)

    return json.dumps({
        "count": len(result_list),
        "projects": result_list,
        "registry_path": str(REGISTRY_PATH),
    }, indent=2)


@mcp.tool()
def workflow_project_remove(project_dir: str) -> str:
    """Remove a project from the registry.

    Does not delete any files — just removes the project from the
    cross-project tracking list.

    Args:
        project_dir: Project directory to remove.
    """
    p = Path(project_dir).resolve()
    registry = _load_registry()
    key = str(p)
    if key in registry["projects"]:
        removed = registry["projects"].pop(key)
        _save_registry(registry)
        return json.dumps({
            "removed": True,
            "project_dir": key,
            "name": removed.get("name"),
        }, indent=2)
    return json.dumps({
        "removed": False,
        "project_dir": key,
        "message": "Project not found in registry.",
    }, indent=2)


def _read_json_safe(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, ValueError):
            pass
    return {}


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def workflow_init(
    project_type: str | None = None,
    from_migration: bool = False,
    project_dir: str | None = None,
) -> str:
    """Initialize a new delivery workflow in the project directory.

    Args:
        project_type: "greenfield" or "evolution". Leave empty to determine during intake.
                      Can be defaulted via PRODUCT_DELIVERY_PROJECT_TYPE in .env.
        from_migration: Detect existing BUILD_PLAN.md or ROADMAP.md and migrate.
        project_dir: Project directory. Defaults to working directory.
    """
    p = _resolve(project_dir)
    try:
        if project_type is None:
            project_type = _get_config(p, "PROJECT_TYPE") or None
        result = engine.init_workflow(p, project_type, from_migration)
        _register_project(p, result.get("phase", "intake"))
        return json.dumps(result, indent=2)
    except engine.WorkflowError as e:
        return _error_response(e)


@mcp.tool()
def workflow_status(project_dir: str | None = None) -> str:
    """Get the current workflow state, work items, and recent events.

    Args:
        project_dir: Project directory. Defaults to working directory.
    """
    try:
        result = engine.get_status(_resolve(project_dir))
        return json.dumps(result, indent=2)
    except engine.WorkflowError as e:
        return _error_response(e)


@mcp.tool()
def workflow_next(project_dir: str | None = None) -> str:
    """Show allowed transitions from the current state with guard status.

    Returns which states can be reached and whether each guard passes,
    so the agent can decide what to do next.

    Args:
        project_dir: Project directory. Defaults to working directory.
    """
    try:
        result = engine.get_next_transitions(_resolve(project_dir))
        return json.dumps(result, indent=2)
    except engine.WorkflowError as e:
        return _error_response(e)


@mcp.tool()
def workflow_transition(
    target: str,
    evidence: list[str] | None = None,
    force: bool = False,
    reason: str | None = None,
    project_dir: str | None = None,
) -> str:
    """Attempt a state transition. Guards are checked automatically.

    Args:
        target: Target state (e.g. "discover", "frame", "plan", "deliver").
        evidence: Evidence keys to attach to this transition.
        force: Force past failed guards (logged as forced).
        reason: Human-readable reason for the transition.
        project_dir: Project directory. Defaults to working directory.
    """
    try:
        result = engine.do_transition(_resolve(project_dir), target, evidence, force, reason)
        return json.dumps(result, indent=2)
    except engine.GuardFailedError as e:
        return json.dumps({
            "error": "GuardFailedError",
            "message": str(e),
            "guard_results": e.guard_results,
        })
    except engine.WorkflowError as e:
        return _error_response(e)


@mcp.tool()
def workflow_item_add(
    name: str,
    acceptance_criteria: list[str] | None = None,
    risks: list[str] | None = None,
    project_dir: str | None = None,
) -> str:
    """Add a work item to the delivery plan.

    Only valid in plan or deliver phase. The item starts in "ready" state.

    Args:
        name: Work item name/description.
        acceptance_criteria: AC-NNN IDs this item satisfies.
        risks: RISK-NNN IDs associated with this item.
        project_dir: Project directory. Defaults to working directory.
    """
    try:
        result = engine.add_item(_resolve(project_dir), name, acceptance_criteria, risks)
        return json.dumps(result, indent=2)
    except engine.WorkflowError as e:
        return _error_response(e)


@mcp.tool()
def workflow_item_list(
    state_filter: str | None = None,
    project_dir: str | None = None,
) -> str:
    """List all work items, optionally filtered by state.

    Args:
        state_filter: Only show items in this state (e.g. "ready", "implementing").
        project_dir: Project directory. Defaults to working directory.
    """
    try:
        result = engine.list_items(_resolve(project_dir), state_filter)
        return json.dumps(result, indent=2)
    except engine.WorkflowError as e:
        return _error_response(e)


@mcp.tool()
def workflow_item_transition(
    item_id: str,
    target: str,
    reason: str | None = None,
    force: bool = False,
    project_dir: str | None = None,
) -> str:
    """Transition a work item to a new state.

    Guards require evidence for verifying/accepted transitions (test-results).
    Use workflow_evidence_record / workflow_evidence_test first.

    Args:
        item_id: Work item ID (WI-NNN).
        target: Target state.
        reason: Reason for the transition.
        force: Force past failed guards (logged).
        project_dir: Project directory. Defaults to working directory.
    """
    try:
        result = engine.transition_item(
            _resolve(project_dir), item_id, target, reason, force=force
        )
        return json.dumps(result, indent=2)
    except engine.GuardFailedError as e:
        return json.dumps({
            "error": "GuardFailedError",
            "message": str(e),
            "guard_results": e.guard_results,
        })
    except engine.WorkflowError as e:
        return _error_response(e)


@mcp.tool()
def workflow_evidence_record(
    evidence_type: str,
    content: str | None = None,
    path: str | None = None,
    item_id: str | None = None,
    project_dir: str | None = None,
) -> str:
    """Record structured evidence in the workflow evidence index.

    Types include: brief, synthesis, health-check, maturity-assessment,
    user-confirmation, plan, test-results, coherence-check, eval-results, artifact.

    Args:
        evidence_type: Evidence type string.
        content: Inline text content (written under .workflow/evidence/).
        path: Path to an existing file to copy into evidence storage.
        item_id: Optional WI-NNN to link this evidence to.
        project_dir: Project directory. Defaults to working directory.
    """
    try:
        result = engine.record_evidence(
            _resolve(project_dir),
            evidence_type,
            content=content,
            path=path,
            item_id=item_id,
        )
        return json.dumps(result, indent=2)
    except engine.WorkflowError as e:
        return _error_response(e)


@mcp.tool()
def workflow_evidence_test(
    command: str = "pytest",
    item_id: str | None = None,
    project_dir: str | None = None,
) -> str:
    """Run a test command and record test-results evidence.

    Args:
        command: Shell command to run (default: pytest).
        item_id: Optional WI-NNN; auto-detected if one item is implementing/verifying.
        project_dir: Project directory. Defaults to working directory.
    """
    try:
        result = engine.run_and_record_tests(
            _resolve(project_dir), command, item_id=item_id
        )
        return json.dumps(result, indent=2)
    except engine.WorkflowError as e:
        return _error_response(e)


@mcp.tool()
def workflow_item_waive(
    item_id: str,
    approver: str,
    reason: str,
    project_dir: str | None = None,
) -> str:
    """Waive a work item that is in reviewing state.

    Requires explicit approver and reason. The item moves to "waived"
    terminal state and counts toward the completion guard.

    Args:
        item_id: Work item ID (WI-NNN).
        approver: Who approved the waiver.
        reason: Why the item is being waived.
        project_dir: Project directory. Defaults to working directory.
    """
    try:
        result = engine.waive_item(_resolve(project_dir), item_id, approver, reason)
        return json.dumps(result, indent=2)
    except engine.WorkflowError as e:
        return _error_response(e)


@mcp.tool()
def workflow_check(
    project_dir: str | None = None,
    ci: bool = False,
) -> str:
    """Run guard checks for available transitions, or CI merge gate.

    Args:
        project_dir: Project directory. Defaults to working directory.
        ci: If True, run check_ci (non-zero semantics for merge gates).
    """
    try:
        p = _resolve(project_dir)
        if ci:
            result = engine.check_ci(p)
        else:
            result = engine.check_guards(p)
        return json.dumps(result, indent=2)
    except engine.WorkflowError as e:
        return _error_response(e)


@mcp.tool()
def workflow_block(
    reason: str,
    owner: str,
    project_dir: str | None = None,
) -> str:
    """Block the workflow. Records the current phase as the resume target.

    Args:
        reason: Why the workflow is blocked.
        owner: Who is responsible for resolving the blocker.
        project_dir: Project directory. Defaults to working directory.
    """
    try:
        result = engine.block_workflow(_resolve(project_dir), reason, owner)
        return json.dumps(result, indent=2)
    except engine.WorkflowError as e:
        return _error_response(e)


@mcp.tool()
def workflow_resume(
    reason: str | None = None,
    project_dir: str | None = None,
) -> str:
    """Resume a blocked workflow back to its pre-blocked phase.

    Args:
        reason: Why the blocker is resolved.
        project_dir: Project directory. Defaults to working directory.
    """
    try:
        result = engine.resume_workflow(_resolve(project_dir), reason)
        return json.dumps(result, indent=2)
    except engine.WorkflowError as e:
        return _error_response(e)


@mcp.tool()
def workflow_waive_guard(
    guard: str,
    approver: str,
    reason: str,
    project_dir: str | None = None,
) -> str:
    """Waive a transition guard so the transition can proceed.

    The waiver is recorded with the approver and reason. The guard
    will show as "waived" instead of "fail" on subsequent checks.

    Args:
        guard: Guard name to waive (from workflow_check results).
        approver: Who approved the waiver.
        reason: Why the guard is being waived (risk accepted).
        project_dir: Project directory. Defaults to working directory.
    """
    try:
        result = engine.waive_guard(_resolve(project_dir), guard, approver, reason)
        return json.dumps(result, indent=2)
    except engine.WorkflowError as e:
        return _error_response(e)


@mcp.tool()
def workflow_close(
    reason: str | None = None,
    force: bool = False,
    project_dir: str | None = None,
) -> str:
    """Close the workflow (normally from stabilize phase).

    Args:
        reason: Reason for closing.
        force: Close from a non-stabilize phase (logged as forced).
        project_dir: Project directory. Defaults to working directory.
    """
    try:
        result = engine.close_workflow(_resolve(project_dir), reason, force)
        return json.dumps(result, indent=2)
    except engine.WorkflowError as e:
        return _error_response(e)


@mcp.tool()
def workflow_render(project_dir: str | None = None) -> str:
    """Render a human-readable markdown summary of the workflow status.

    Args:
        project_dir: Project directory. Defaults to working directory.
    """
    try:
        return engine.render_status(_resolve(project_dir))
    except engine.WorkflowError as e:
        return _error_response(e)


@mcp.tool()
def workflow_config(project_dir: str | None = None) -> str:
    """Show the active configuration from .env and environment variables.

    Returns all PRODUCT_DELIVERY_* settings and where they came from.

    Args:
        project_dir: Project directory. Defaults to working directory.
    """
    p = _resolve(project_dir)
    config = _load_env(p)
    return json.dumps({
        "project_dir": str(p),
        "config": config,
        "env_file": str(p / ".env"),
        "env_file_exists": (p / ".env").exists(),
    }, indent=2)


@mcp.tool()
def workflow_setup(
    dry_run: bool = True,
    project_dir: str | None = None,
) -> str:
    """Set up product-delivery in a project.

    Creates or updates: .claude/settings.json, .cursor/mcp.json,
    AGENTS.md (workflow rule), .env.sample, .env, and .gitignore.
    Merges into existing files without clobbering other settings.
    Idempotent — safe to run multiple times.

    Call with dry_run=True first to preview changes, then with
    dry_run=False to apply them. Present the preview to the user
    before applying.

    Args:
        dry_run: If True, preview what would change without writing.
                 If False, apply the changes.
        project_dir: Project directory. Defaults to working directory.
    """
    from . import setup

    p = _resolve(project_dir)

    if dry_run:
        result = setup.plan_setup(p)
        result["mode"] = "preview"
        if result["already_setup"]:
            result["message"] = "Project is already set up for product-delivery. No changes needed."
        else:
            result["message"] = (
                f"Setup will make {result['changes_count']} change(s) to "
                f"{result['project_name']}. Call workflow_setup(dry_run=False) "
                f"to apply."
            )
    else:
        result = setup.execute_setup(p)
        result["mode"] = "applied"
        result["message"] = (
            f"Setup complete. {result['applied_count']} file(s) changed. "
            + (" ".join(result["next_steps"]))
        )
        _register_project(p)

    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------

def _read_package_file(rel_path: str) -> str:
    full = PACKAGE_ROOT / rel_path
    if full.exists():
        return full.read_text()
    return f"File not found: {rel_path}"


@mcp.resource("workflow://state")
def resource_state() -> str:
    """Current workflow state (state.json contents)."""
    state_path = DEFAULT_PROJECT_DIR / ".workflow" / "state.json"
    if state_path.exists():
        return state_path.read_text()
    return '{"error": "No workflow initialized"}'


@mcp.resource("workflow://events")
def resource_events() -> str:
    """Workflow event log (events.jsonl contents)."""
    events_path = DEFAULT_PROJECT_DIR / ".workflow" / "events.jsonl"
    if events_path.exists():
        return events_path.read_text()
    return ""


@mcp.resource("workflow://template/brief")
def resource_template_brief() -> str:
    """Project brief template."""
    return _read_package_file("templates/brief.md")


@mcp.resource("workflow://template/plan")
def resource_template_plan() -> str:
    """Delivery plan template (greenfield sequenced steps / evolution priority buckets)."""
    return _read_package_file("templates/plan.md")


@mcp.resource("workflow://template/evidence-ledger")
def resource_template_evidence() -> str:
    """Evidence tracking template."""
    return _read_package_file("templates/evidence-ledger.md")


@mcp.resource("workflow://reference/lifecycle")
def resource_ref_lifecycle() -> str:
    """Full state machine specification and transition table."""
    return _read_package_file("references/lifecycle.md")


@mcp.resource("workflow://reference/guards")
def resource_ref_guards() -> str:
    """Guard catalog for every transition."""
    return _read_package_file("references/guards.md")


@mcp.resource("workflow://reference/evidence")
def resource_ref_evidence() -> str:
    """Evidence types and linking rules."""
    return _read_package_file("references/evidence.md")


@mcp.resource("workflow://reference/harness-compatibility")
def resource_ref_compat() -> str:
    """Capability matrix across AI coding harnesses."""
    return _read_package_file("references/harness-compatibility.md")


@mcp.resource("workflow://reference/migration")
def resource_ref_migration() -> str:
    """Migration guide from BUILD_PLAN.md / ROADMAP.md."""
    return _read_package_file("references/migration.md")


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

@mcp.prompt()
def product_delivery() -> str:
    """Load the product-delivery conversational skill instructions.

    This prompt provides the agent with the full conversational skill:
    how to run discovery, when to push back, pacing rules, evidence
    types, and interaction style. Load it into context when starting
    or resuming a product delivery workflow.
    """
    return _read_package_file("SKILL.md")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(project_dir: Path | None = None):
    global DEFAULT_PROJECT_DIR
    if project_dir:
        DEFAULT_PROJECT_DIR = project_dir
    mcp.run()
