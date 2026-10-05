#!/usr/bin/env python3
"""beforeMCPExecution: deny phase/item transitions when guards would fail."""

from __future__ import annotations

import json
import sys
from pathlib import Path

GATE_TOOLS = {
    "workflow_transition",
    "workflow_item_transition",
}


def _allow(msg: str | None = None) -> int:
    out = {"permission": "allow"}
    if msg:
        out["agent_message"] = msg
    print(json.dumps(out))
    return 0


def _deny(agent_message: str, user_message: str | None = None) -> int:
    out = {
        "permission": "deny",
        "agent_message": agent_message,
        "user_message": user_message or agent_message,
    }
    print(json.dumps(out))
    return 0


def _import_engine():
    try:
        from product_delivery import engine
        return engine
    except ImportError:
        pass
    for candidate in (
        Path.cwd() / "src",
        Path(__file__).resolve().parents[3] / "src",
    ):
        if (candidate / "product_delivery").is_dir():
            sys.path.insert(0, str(candidate))
            try:
                from product_delivery import engine
                return engine
            except ImportError:
                continue
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return _allow()

    server = payload.get("mcp_server_name") or ""
    tool = payload.get("tool_name") or ""

    if server and server != "product-delivery":
        return _allow()
    if tool not in GATE_TOOLS:
        return _allow()

    raw_input = payload.get("tool_input") or "{}"
    if isinstance(raw_input, dict):
        params = raw_input
    else:
        try:
            params = json.loads(raw_input)
        except json.JSONDecodeError:
            return _allow()

    project_dir = params.get("project_dir")
    project = Path(project_dir).resolve() if project_dir else Path.cwd().resolve()

    if not (project / ".workflow" / "state.json").exists():
        return _allow()

    engine = _import_engine()
    if engine is None:
        return _deny(
            "product-delivery package not importable; cannot evaluate guards."
        )

    if params.get("force"):
        return _allow("force=True — guards bypassed (logged by engine).")

    try:
        if tool == "workflow_transition":
            target = params.get("target")
            if not target:
                return _allow()
            preview = engine.preview_transition(project, target)
        else:
            item_id = params.get("item_id")
            target = params.get("target")
            if not item_id or not target:
                return _allow()
            if target in ("cancelled", "rework", "blocked"):
                return _allow()
            preview = engine.preview_item_transition(project, item_id, target)
    except Exception as e:
        return _deny(f"Guard preview failed: {e}")

    if preview.get("allowed"):
        return _allow()

    failed = preview.get("failed") or []
    msg = (
        f"Guards blocked {tool} → {params.get('target')}: {', '.join(failed)}. "
        "Record evidence (workflow_evidence_test / workflow_evidence_record) "
        "or waive the guard before retrying."
    )
    return _deny(msg)


if __name__ == "__main__":
    raise SystemExit(main())
