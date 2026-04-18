# MOCAgent session finalization

You've analyzed the `moc_audit` report, created necessary hubs/
technologies/concepts, linked them under MOC, filled the intro. Time
to close the session.

## HARD CONTRACT

- **You have limited iteration budget** (`max_tool_iterations`).
- **`submit_plan` is the only terminator.** Without it session fails
  validation, plan goes to trash, retry from scratch.
- **Hard enforcement:** on final iterations provider gets
  `tool_choice={"type":"tool","name":"submit_plan"}` and won't let you
  call anything else.
- **Typical flow:** 1 iteration `moc_audit` → 2-5 iterations of
  `create_*` + `add_moc_link` + `add_related_link` (each parallel batch)
  → 1 iteration `moc_set_intro` → `submit_plan`. Total **5-10 iterations**.

## Batch in parallel

`parallel_tool_calls=True` — in one turn emit multiple independent calls:

- **All `create_hub` at once** (6 hubs = 1 turn).
- **All `create_technology` at once** once you know which.
- **All `add_moc_link` at once** — linking hubs/technologies/concepts
  to MOC sections.
- **All `add_related_link` at once** — cross-linking hubs with their modules.

Anti-pattern: one `create_hub` → turn → another `create_hub` → turn.
Burns budget for nothing.

## When audit shows "all OK"

If `moc_audit` returned no hub suggestions, no orphans, no missing
sections — **don't invent actions**. Call immediately:

```
submit_plan(summary="MOC audit clean — no changes.")
```

and we're done. Empty session is fully allowed.

## `submit_plan` format

- `summary`: 1-3 sentences on what you did and why (e.g. "Added topical
  hubs Agent, Git, Logs, Mcp, Providers, Vault grouping 54 modules.
  Created 8 technology notes: FastAPI, Pydantic, httpx, openai,
  anthropic, rich, pyyaml, GitPython. Filled MOC intro.").

`submit_plan` **doesn't take an action list** — already registered by
individual tool calls. Summary goes to preview + vault commit message
(prefix `Agent-MOC:`).
