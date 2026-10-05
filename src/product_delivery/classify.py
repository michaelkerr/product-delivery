"""Project classification for bootstrap / detect playbooks."""

from __future__ import annotations

from pathlib import Path

ESTABLISHED_MARKERS = (
    "package.json",
    "pyproject.toml",
    "Cargo.toml",
    "go.mod",
    "pom.xml",
    "Gemfile",
    "composer.json",
    "AGENTS.md",
    "src",
    "lib",
    "app",
)


def _has_mcp_or_agents(project_dir: Path) -> tuple[bool, bool]:
    has_agents = False
    agents = project_dir / "AGENTS.md"
    if agents.exists():
        try:
            has_agents = "product-delivery" in agents.read_text()
        except OSError:
            has_agents = False

    has_mcp = False
    for sub, name in ((".claude", "settings.json"), (".cursor", "mcp.json")):
        path = project_dir / sub / name
        if not path.exists():
            continue
        try:
            text = path.read_text()
        except OSError:
            continue
        if "product-delivery" in text:
            has_mcp = True
            break
    return has_mcp, has_agents


def _looks_established(project_dir: Path) -> bool:
    for name in ESTABLISHED_MARKERS:
        if (project_dir / name).exists():
            return True
    return False


def classify_project(project_dir: Path) -> dict:
    """Classify a project and return a typed path with next_actions playbook."""
    project_dir = project_dir.resolve()
    has_workflow = (project_dir / ".workflow" / "state.json").exists()
    has_build_plan = (project_dir / "BUILD_PLAN.md").exists()
    has_roadmap = (project_dir / "ROADMAP.md").exists()
    has_mcp, has_agents = _has_mcp_or_agents(project_dir)
    has_env = (project_dir / ".env").exists()
    has_hooks = (project_dir / ".cursor" / "hooks.json").exists()
    setup_present = has_mcp or has_agents
    established = _looks_established(project_dir)

    details = {
        "has_workflow": has_workflow,
        "has_mcp_config": has_mcp,
        "has_agents_rule": has_agents,
        "has_hooks": has_hooks,
        "has_env": has_env,
        "has_build_plan": has_build_plan,
        "has_roadmap": has_roadmap,
        "looks_established": established,
    }

    if has_workflow:
        return {
            "path": "resume",
            "state": "active_workflow",
            "phase": None,  # filled by caller
            "action": "Workflow exists. Call workflow_status then workflow_next.",
            "next_actions": [
                {"tool": "workflow_status", "args": {}},
                {"tool": "workflow_next", "args": {}},
                {
                    "instruction": (
                        "Load the product_delivery prompt and continue from the "
                        "current phase. Do not re-run setup or init."
                    )
                },
            ],
            "details": details,
        }

    needs_setup = not setup_present
    setup_actions = []
    if needs_setup:
        setup_actions.append({"tool": "workflow_setup", "args": {"dry_run": False}})

    if has_build_plan or has_roadmap:
        legacy = []
        if has_build_plan:
            legacy.append("BUILD_PLAN.md")
        if has_roadmap:
            legacy.append("ROADMAP.md")
        return {
            "path": "legacy_migration",
            "state": "legacy_migration" if setup_present else "not_setup",
            "phase": None,
            "action": (
                f"Found legacy artifact(s): {', '.join(legacy)}. "
                "Follow next_actions to set up (if needed) and migrate."
            ),
            "next_actions": setup_actions + [
                {"tool": "workflow_init", "args": {"from_migration": True}},
                {"tool": "workflow_status", "args": {}},
                {
                    "instruction": (
                        "Load the product_delivery prompt; continue from the "
                        "inferred phase. Do not re-ask greenfield intake."
                    )
                },
            ],
            "details": details,
        }

    if established:
        return {
            "path": "established_evolution",
            "state": "setup_no_workflow" if setup_present else "not_setup",
            "phase": None,
            "action": (
                "Established codebase without legacy plan artifacts. "
                "Set up (if needed), init as evolution, then health-check discover."
            ),
            "next_actions": setup_actions + [
                {"tool": "workflow_init", "args": {"project_type": "evolution"}},
                {"tool": "workflow_status", "args": {}},
                {
                    "instruction": (
                        "Load the product_delivery prompt. Run a codebase health "
                        "check, record evidence type health-check, then continue "
                        "discover → frame. Skip full greenfield intake."
                    )
                },
            ],
            "details": details,
        }

    return {
        "path": "greenfield",
        "state": "setup_no_workflow" if setup_present else "not_setup",
        "phase": None,
        "action": (
            "Brand-new / empty project. Set up (if needed), init as greenfield, "
            "then run intake conversation."
        ),
        "next_actions": setup_actions + [
            {"tool": "workflow_init", "args": {"project_type": "greenfield"}},
            {"tool": "workflow_status", "args": {}},
            {
                "instruction": (
                    "Load the product_delivery prompt and run the greenfield "
                    "intake conversation (one question at a time)."
                )
            },
        ],
        "details": details,
    }
