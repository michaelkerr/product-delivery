# Product Delivery

A unified lifecycle skill for software products — from first idea through delivery and stabilization. Replaces the separate product-discovery and product-evolution skills with a single hierarchical state machine.

## What it does

Manages the full product lifecycle through ten states: **intake → discover → frame → plan → deliver → release → stabilize → closed**, plus **blocked** (resumable overlay) and **aborted** (terminal). A nested work-item submachine inside `deliver` tracks individual items through **ready → implementing → verifying → reviewing → accepted**.

Every transition is evidence-gated: guards check that required artifacts and recorded evidence exist before allowing a state change. State lives in `.workflow/state.json` with an append-only event log in `.workflow/events.jsonl`. Cursor hooks capture test runs and block MCP transitions that fail guards.

## Add to a repo

Works for brand-new and established projects:

```bash
# From the target project
uvx --from product-delivery product-delivery setup
# Or in an agent session: workflow_detect → follow next_actions
# Or one-shot: workflow_bootstrap
```

| Situation | What happens |
|---|---|
| Empty / new repo | Setup (MCP + hooks + AGENTS) → greenfield init → intake |
| Has `ROADMAP.md` / `BUILD_PLAN.md` | Setup → migrate into work items → resume inferred phase |
| Established codebase, no legacy plan | Setup → evolution init → health-check discover |
| Already has `.workflow/` | Resume via `workflow_status` / `workflow_next` |

CI gate for consumers (installed by setup when missing):

```bash
python scripts/workflow check --ci
# or: workflow_check(ci=True)
```

## Installation

### MCP Server (recommended)

The MCP server gives any MCP-capable harness identical behavior — tools, resources, and a prompt for loading the skill instructions.

```bash
# Install and run via uvx (no clone needed)
uvx product-delivery

# Or install from source
pip install -e .
product-delivery

# With a specific project directory
product-delivery --project-dir /path/to/project
```

**Claude Code** — add to your MCP config:

```json
{
  "mcpServers": {
    "product-delivery": {
      "command": "uvx",
      "args": ["product-delivery"]
    }
  }
}
```

**Cursor / other MCP clients** — same pattern, point to the `product-delivery` command.

### Claude Plugin

```bash
# Install the .plugin file in Claude Code
claude plugin install product-delivery.plugin
```

### CLI Only

For environments without MCP, the CLI wrapper provides the same engine:

```bash
python scripts/workflow init
python scripts/workflow status
python scripts/workflow transition discover
```

### Manual (any harness)

Read `SKILL.md` and follow the instructions. The skill is harness-agnostic — no vendor tool names. Without MCP or CLI, state management is advisory.

## MCP Server Reference

### Tools (19)

| Tool | Purpose |
|------|---------|
| `workflow_detect` | Detect project path + next_actions playbook |
| `workflow_bootstrap` | Apply setup + init/migrate in one step |
| `workflow_projects` | List all registered projects across directories |
| `workflow_project_remove` | Remove a project from the registry |
| `workflow_init` | Initialize a new delivery workflow |
| `workflow_status` | Get current state, work items, events |
| `workflow_next` | Show allowed transitions with guard status |
| `workflow_transition` | Attempt a state transition (guard-checked) |
| `workflow_item_add` | Add a work item to the plan |
| `workflow_item_list` | List work items (filterable by state) |
| `workflow_item_transition` | Transition a work item |
| `workflow_item_waive` | Waive a work item in review |
| `workflow_evidence_record` | Record structured evidence |
| `workflow_evidence_test` | Run tests and record test-results evidence |
| `workflow_check` | Run guard checks (`ci=True` for merge gate) |
| `workflow_block` | Block the workflow |
| `workflow_resume` | Resume from blocked |
| `workflow_waive_guard` | Waive a transition guard |
| `workflow_close` | Close the workflow |
| `workflow_render` | Render a markdown status summary |
| `workflow_config` | Show active configuration from .env |
| `workflow_setup` | Set up product-delivery in a project (dry_run preview + apply) |

### Resources (10)

| URI | Content |
|-----|---------|
| `workflow://state` | Current state.json |
| `workflow://events` | Event log |
| `workflow://template/brief` | Project brief template |
| `workflow://template/plan` | Delivery plan template |
| `workflow://template/evidence-ledger` | Evidence tracking template |
| `workflow://reference/lifecycle` | State machine specification |
| `workflow://reference/guards` | Guard catalog |
| `workflow://reference/evidence` | Evidence types and rules |
| `workflow://reference/harness-compatibility` | Harness capability matrix |
| `workflow://reference/migration` | Migration guide |

### Prompts (1)

| Prompt | Purpose |
|--------|---------|
| `product_delivery` | Load the full conversational skill instructions (SKILL.md) |

## Migration from legacy skills

Projects with existing BUILD_PLAN.md or ROADMAP.md can migrate:

```bash
# Via MCP
workflow_init(from_migration=True)

# Via CLI
python scripts/workflow init --from-migration
```

The engine detects which artifact exists, infers the current state from artifact content, creates `.workflow/state.json`, and logs a migration event. Legacy artifacts are preserved.
