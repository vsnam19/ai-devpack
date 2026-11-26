# Terminal dashboard mockup

Run `"$TUI_PY" -m runner.dash.tui --demo` to browse two fixture plans, inspect task and agent briefs, watch the simulated event stream, and review gates, dependencies, metadata, and a pending approval. The plan progress bar tracks tasks that have started, with the completion count shown in the plan brief. At 80×24, click a panel title or press its number to expand it. To regenerate the captures, run `"$TUI_PY" -m runner.dash.tui --demo --screenshots docs/design/tui-mockup`.

## Key map

| Key | Action |
| --- | --- |
| `1`–`4` | Focus Plans, Tasks, Agents, or Approvals |
| `Tab` / `Shift+Tab`, `h` / `l` | Cycle through the sidebar and detail panels |
| `j` / `k`, `↑` / `↓` | Move through rows or scroll details |
| `Enter` | Drill into the highlighted row |
| `[` / `]` | Switch detail tabs |
| `f` | Toggle log follow |
| `/` | Filter the current list |
| `Esc` | Close help or clear the current filter |
| `a` | Jump to approvals |
| `Ctrl+T` | Switch between dark and light themes |
| `?` | Open the keyboard help |
| `q` | Quit |

## Screenshots

- [Overview](01-overview.svg)
- [Running agent log](02-task-agents-log.svg)
- [Failed gate on attempt 1](03-gate-failure.svg)
- [Plan dependency graph](04-plan-graph.svg)
- [Pending approval](05-approvals.svg)
- [Keyboard help](06-help.svg)
- [Compact layout at 80×24](07-80x24.svg)
