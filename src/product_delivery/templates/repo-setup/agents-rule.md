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
