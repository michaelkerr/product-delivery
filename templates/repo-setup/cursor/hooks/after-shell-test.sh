#!/usr/bin/env python3
"""afterShellExecution: record test-results evidence for test runner commands."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

TEST_PATTERNS = re.compile(
    r"\b(pytest|py\.test|npm\s+test|npx\s+.*test|cargo\s+test|"
    r"go\s+test|mvn\s+test|gradlew?\s+test|bun\s+test|vitest|"
    r"jest|python\s+-m\s+pytest)\b",
    re.I,
)


def _import_engine():
    try:
        from product_delivery import engine
        return engine
    except ImportError:
        pass
    # Editable / source checkout next to common paths
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
        return 0

    command = payload.get("command") or ""
    output = payload.get("output") or ""
    cwd = payload.get("cwd") or str(Path.cwd())
    project = Path(cwd).resolve()

    if not TEST_PATTERNS.search(command):
        return 0
    if not (project / ".workflow" / "state.json").exists():
        return 0

    exit_code = 0
    passed = failed = None
    m = re.search(r"(\d+)\s+passed", output)
    if m:
        passed = int(m.group(1))
    m = re.search(r"(\d+)\s+failed", output)
    if m:
        failed = int(m.group(1))
        if failed > 0:
            exit_code = 1
    if re.search(r"exit code [1-9]", output, re.I):
        exit_code = 1

    engine = _import_engine()
    if engine is None:
        # Last resort: CLI if installed
        try:
            subprocess.run(
                [
                    sys.executable, "-c",
                    "from product_delivery import engine; "
                    f"engine.record_test_results(__import__('pathlib').Path({str(project)!r}), "
                    f"command={command!r}, exit_code={exit_code}, "
                    f"stdout_tail={output[-4000:]!r}, passed={passed!r}, failed={failed!r})",
                ],
                check=False,
                capture_output=True,
            )
        except Exception:
            pass
        return 0

    try:
        engine.record_test_results(
            project,
            command=command,
            exit_code=exit_code,
            stdout_tail=output[-4000:],
            passed=passed,
            failed=failed,
        )
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
