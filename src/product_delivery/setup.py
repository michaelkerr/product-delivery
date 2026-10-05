"""Project setup — wire up product-delivery in a repo.

Called via:
    - MCP tool: workflow_setup (from within a harness conversation)
    - CLI: product-delivery setup
    - Standalone: python scripts/setup
"""

import json
import shutil
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MCP_SERVER_CONFIG = {
    "command": "uvx",
    "args": ["product-delivery"],
}

AGENTS_RULE = """\
## Development Workflow

This project uses the **product-delivery** lifecycle. Evidence and
transitions are **machine-checked** — not advisory.

Before starting any work:

1. Call `workflow_detect` first. Execute its `next_actions` in order.
2. If no workflow exists, follow the playbook (setup → init/migrate).
3. Load the `product_delivery` prompt for full skill instructions.

Rules:

- Follow workflow transitions. Do not skip states or bypass guards
  without an explicit waiver.
- Use `workflow_next` / `workflow_check` to see what is allowed.
- Transition work items through:
  ready → implementing → verifying → reviewing → accepted.
- Run tests before verifying and before accepting; Cursor hooks auto-
  record pytest (and similar) runs as `test-results` evidence.
- Record evidence (`workflow_evidence_record`) for confirmations,
  health checks, and artifacts before advancing phases.
- Update AGENTS.md when new patterns emerge during delivery.
"""

ENV_SAMPLE = """\
# Product Delivery Configuration
# Copy to .env and customize. Do not commit .env to version control.
#
# All variables are prefixed PRODUCT_DELIVERY_ and are optional.
# Precedence: environment variables > .env file > defaults.

# Default project type for workflow init (greenfield or evolution).
# Leave blank to determine during intake conversation.
# PRODUCT_DELIVERY_PROJECT_TYPE=greenfield

# Guard enforcement mode:
#   strict - guards must pass or be explicitly waived (default)
#   warn   - log failures but allow transitions
#   skip   - disable guard checks entirely
# PRODUCT_DELIVERY_GUARD_MODE=strict

# Comma-separated states to skip during init (only discover and frame
# are skippable). Useful for small projects that don't need full discovery.
# PRODUCT_DELIVERY_SKIP_STATES=
"""

GITIGNORE_LINES = [".env", ".workflow/"]
MARKER = "product-delivery** lifecycle"

HOOKS_JSON = {
    "version": 1,
    "hooks": {
        "afterShellExecution": [
            {
                "command": ".cursor/hooks/after-shell-test.sh",
                "matcher": "pytest|npm test|cargo test|go test|vitest|jest",
            }
        ],
        "beforeMCPExecution": [
            {
                "command": ".cursor/hooks/before-mcp-transition.sh",
                "failClosed": True,
                "matcher": "workflow_transition|workflow_item_transition",
            }
        ],
    },
}

PACKAGE_ROOT = Path(__file__).resolve().parent


def _templates_dir() -> Path:
    bundled = PACKAGE_ROOT / "templates" / "repo-setup"
    if bundled.exists():
        return bundled
    # Dev checkout fallback (repo root / templates / repo-setup)
    return PACKAGE_ROOT.parent.parent / "templates" / "repo-setup"


def _read_json(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, ValueError):
            return {}
    return {}


def _write_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


# ---------------------------------------------------------------------------
# Setup steps
# ---------------------------------------------------------------------------

def _check_mcp_config(project: Path, subdir: str, filename: str) -> dict:
    config_path = project / subdir / filename
    existing = _read_json(config_path)
    servers = existing.get("mcpServers", {})

    if "product-delivery" in servers:
        return {"file": f"{subdir}/{filename}", "action": "skip",
                "detail": "already configured"}

    action = "update" if config_path.exists() else "create"
    other_servers = [k for k in servers if k != "product-delivery"]
    return {"file": f"{subdir}/{filename}", "action": action,
            "detail": f"add product-delivery server"
                       + (f" (alongside {', '.join(other_servers)})" if other_servers else "")}


def _apply_mcp_config(project: Path, subdir: str, filename: str):
    config_path = project / subdir / filename
    existing = _read_json(config_path)
    servers = existing.get("mcpServers", {})
    servers["product-delivery"] = MCP_SERVER_CONFIG.copy()
    if subdir == ".cursor":
        servers["product-delivery"]["envFile"] = "${workspaceFolder}/.env"
    existing["mcpServers"] = servers
    _write_json(config_path, existing)


def _check_agents_md(project: Path) -> dict:
    agents_path = project / "AGENTS.md"
    if agents_path.exists():
        if MARKER in agents_path.read_text():
            return {"file": "AGENTS.md", "action": "skip",
                    "detail": "workflow rule already present"}
        return {"file": "AGENTS.md", "action": "update",
                "detail": "append workflow rule to existing file"}
    return {"file": "AGENTS.md", "action": "create",
            "detail": "create with workflow rule"}


def _apply_agents_md(project: Path):
    agents_path = project / "AGENTS.md"
    if agents_path.exists():
        with open(agents_path, "a") as f:
            f.write("\n\n" + AGENTS_RULE)
    else:
        agents_path.write_text(f"# {project.name}\n\n{AGENTS_RULE}")


def _check_env(project: Path) -> list[dict]:
    results = []
    if not (project / ".env.sample").exists():
        results.append({"file": ".env.sample", "action": "create",
                        "detail": "configuration template"})
    else:
        results.append({"file": ".env.sample", "action": "skip",
                        "detail": "already exists"})

    if not (project / ".env").exists():
        results.append({"file": ".env", "action": "create",
                        "detail": "local config (gitignored)"})
    else:
        results.append({"file": ".env", "action": "skip",
                        "detail": "already exists"})
    return results


def _apply_env(project: Path):
    if not (project / ".env.sample").exists():
        (project / ".env.sample").write_text(ENV_SAMPLE)
    if not (project / ".env").exists():
        (project / ".env").write_text(ENV_SAMPLE)


def _check_gitignore(project: Path) -> dict:
    gi_path = project / ".gitignore"
    existing = gi_path.read_text().splitlines() if gi_path.exists() else []
    missing = [l for l in GITIGNORE_LINES if l not in existing]
    if not missing:
        return {"file": ".gitignore", "action": "skip",
                "detail": "entries already present"}
    return {"file": ".gitignore", "action": "update",
            "detail": f"add {', '.join(missing)}"}


def _apply_gitignore(project: Path):
    gi_path = project / ".gitignore"
    existing = gi_path.read_text().splitlines() if gi_path.exists() else []
    missing = [l for l in GITIGNORE_LINES if l not in existing]
    if missing:
        with open(gi_path, "a") as f:
            if existing and existing[-1].strip():
                f.write("\n")
            f.write("\n".join(missing) + "\n")


def _check_hooks(project: Path) -> dict:
    hooks_json = project / ".cursor" / "hooks.json"
    hook_a = project / ".cursor" / "hooks" / "after-shell-test.sh"
    hook_b = project / ".cursor" / "hooks" / "before-mcp-transition.sh"
    if hooks_json.exists() and hook_a.exists() and hook_b.exists():
        return {"file": ".cursor/hooks", "action": "skip",
                "detail": "hooks already present"}
    action = "update" if hooks_json.exists() else "create"
    return {"file": ".cursor/hooks", "action": action,
            "detail": "install afterShell + beforeMCP enforcement hooks"}


def _apply_hooks(project: Path):
    hooks_dir = project / ".cursor" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)

    hooks_path = project / ".cursor" / "hooks.json"
    existing = _read_json(hooks_path)
    if not existing:
        existing = {"version": 1, "hooks": {}}
    existing.setdefault("version", 1)
    existing.setdefault("hooks", {})

    # Merge without clobbering unrelated hooks
    for event, entries in HOOKS_JSON["hooks"].items():
        current = existing["hooks"].setdefault(event, [])
        for entry in entries:
            cmd = entry["command"]
            if not any(e.get("command") == cmd for e in current):
                current.append(dict(entry))

    _write_json(hooks_path, existing)

    tmpl = _templates_dir() / "cursor" / "hooks"
    for name in ("after-shell-test.sh", "before-mcp-transition.sh"):
        src = tmpl / name
        dest = hooks_dir / name
        if src.exists():
            dest.write_text(src.read_text())
        try:
            dest.chmod(dest.stat().st_mode | 0o111)
        except OSError:
            pass


def _check_ci_workflow(project: Path) -> dict:
    path = project / ".github" / "workflows" / "product-delivery-check.yml"
    if path.exists():
        return {"file": ".github/workflows/product-delivery-check.yml",
                "action": "skip", "detail": "already present"}
    return {"file": ".github/workflows/product-delivery-check.yml",
            "action": "create", "detail": "consumer CI gate template"}


def _apply_ci_workflow(project: Path):
    tmpl = _templates_dir() / "github" / "product-delivery-check.yml"
    dest = project / ".github" / "workflows" / "product-delivery-check.yml"
    if dest.exists():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    if tmpl.exists():
        dest.write_text(tmpl.read_text())


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def plan_setup(project: Path) -> dict:
    """Preview what setup would do without making changes."""
    steps = []
    steps.append(_check_mcp_config(project, ".claude", "settings.json"))
    steps.append(_check_mcp_config(project, ".cursor", "mcp.json"))
    steps.append(_check_agents_md(project))
    steps.extend(_check_env(project))
    steps.append(_check_gitignore(project))
    steps.append(_check_hooks(project))
    steps.append(_check_ci_workflow(project))

    changes = [s for s in steps if s["action"] != "skip"]
    unchanged = [s for s in steps if s["action"] == "skip"]

    has_workflow = (project / ".workflow" / "state.json").exists()

    return {
        "project_dir": str(project),
        "project_name": project.name,
        "steps": steps,
        "changes_count": len(changes),
        "unchanged_count": len(unchanged),
        "has_existing_workflow": has_workflow,
        "already_setup": len(changes) == 0,
    }


def execute_setup(project: Path) -> dict:
    """Run setup — create/update all config files."""
    results = []

    for subdir, filename in [(".claude", "settings.json"), (".cursor", "mcp.json")]:
        check = _check_mcp_config(project, subdir, filename)
        if check["action"] != "skip":
            _apply_mcp_config(project, subdir, filename)
            check["applied"] = True
        results.append(check)

    check = _check_agents_md(project)
    if check["action"] != "skip":
        _apply_agents_md(project)
        check["applied"] = True
    results.append(check)

    for env_check in _check_env(project):
        results.append(env_check)
    _apply_env(project)

    check = _check_gitignore(project)
    if check["action"] != "skip":
        _apply_gitignore(project)
        check["applied"] = True
    results.append(check)

    check = _check_hooks(project)
    if check["action"] != "skip":
        _apply_hooks(project)
        check["applied"] = True
    results.append(check)

    check = _check_ci_workflow(project)
    if check["action"] != "skip":
        _apply_ci_workflow(project)
        check["applied"] = True
    results.append(check)

    applied = [r for r in results if r.get("applied")]
    skipped = [r for r in results if r["action"] == "skip"]

    has_workflow = (project / ".workflow" / "state.json").exists()

    return {
        "project_dir": str(project),
        "project_name": project.name,
        "results": results,
        "applied_count": len(applied),
        "skipped_count": len(skipped),
        "has_existing_workflow": has_workflow,
        "next_steps": (
            ["Call workflow_detect and follow next_actions",
             "Or call workflow_bootstrap to setup+init in one step",
             "Load the product_delivery prompt for skill instructions"]
            if not has_workflow else
            ["Run workflow_status to check current state",
             "Load the product_delivery prompt to resume"]
        ),
    }


def run_setup(project: Path):
    """CLI-friendly setup with printed output."""
    print(f"\n  Product Delivery Setup")
    print(f"  Project: {project}\n")

    if not shutil.which("uv") and not shutil.which("uvx"):
        print("  ! uv/uvx not found. Install: https://docs.astral.sh/uv/\n")

    result = execute_setup(project)

    for r in result["results"]:
        symbol = {"create": "+", "update": "~", "skip": "·"}
        s = symbol.get(r["action"], " ")
        msg = f"  {s} {r['file']}"
        if r.get("detail"):
            msg += f"  ({r['detail']})"
        print(msg)

    print(f"\n  Done: {result['applied_count']} changed, "
          f"{result['skipped_count']} unchanged.")

    if result["applied_count"]:
        print("\n  Next steps:")
        print("    1. Review .env and uncomment settings you want")
        print("    2. Commit .env.sample, .claude/, .cursor/, AGENTS.md")
        print("    3. Open the project in Claude Code or Cursor")
        print("    4. Agent: call workflow_detect (or workflow_bootstrap)")

    print()
