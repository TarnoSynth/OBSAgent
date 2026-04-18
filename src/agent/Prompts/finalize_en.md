# Documentation plan finalization (FINALIZE)

You have already analyzed **all** diff fragments of this commit in prior conversation turns — you returned summaries of each chunk. Those summaries are collected below together with the vault map.

Now **work in a tool-use loop**: call vault_read tools (exploration) and vault_write tools (writing) iteratively, and at the end call `submit_plan` EXACTLY ONCE with a short `summary`.

## HARD LOOP CONTRACT — read before the first tool call

- **You have a **bounded iteration budget** (`max_tool_iterations`, usually 20).** One iteration = one AI provider call + the tool calls inside it.
- **`submit_plan` is the only terminator.** Without it the session fails validation, the plan is discarded, and retry restarts from scratch.
- **As a help**, the loop will append `[budzet-petli: iteration X/N, remaining M]` to `tool_result` content when you get close to the limit. When you see `remaining <= 2` — **the next turn must be `submit_plan`**.
- **Hard force:** on the final iterations the provider receives `tool_choice={"type":"tool","name":"submit_plan"}` and **won't** let you call anything else. If you end up there, something went wrong — plan the termination earlier.
- **Typical sensible flow:** 1-2 read-only exploration turns → 2-5 write turns → `submit_plan`. Total **5-10 iterations** for most commits. If you feel you need 15+ — you're probably duplicating work.

## Batching rule — save iterations

You run with **`parallel_tool_calls=True`** — in one turn you **CAN and SHOULD** emit multiple `tool_use` blocks at once whenever they are independent. Each turn is a full provider AI call with the accumulated context (30–100s latency on Opus), so minimize the number of turns.

**When to call in parallel (same turn):**

- **Independent reads.** `read_note` on 3 different files → 3 tool calls in **one** turn, not three turns. Same for `list_tags` + `vault_map` + `find_related` done together.
- **Writes on different files.** `create_module("A.md")` + `create_module("B.md")` + `create_module("C.md")` → **one** turn, not three.
- **Multiple granular ops on the SAME file.** Updating a hub? `replace_section` + `add_table_row` + `add_related_link` + `update_frontmatter` all in **one** turn.
- **Changelog + modules together.** `create_changelog_entry` + all `create_module` for this commit → **one** turn.

**When you MUST iterate sequentially (separate turns):**

- You need the result of one tool to build arguments for the next: e.g. `find_related`/`list_notes` first to decide between `create_X` and `replace_section`.
- Standard pattern: 1 exploration turn (many parallel `list_notes`/`read_note`), then 1–2 write turns (parallel `create_*`/`append_section`), then `submit_plan`.

**Target flow after batching:** 3–5 iterations total (1 parallel exploration → 1–2 parallel writes → `submit_plan`). If you're at 10+ iterations, you are almost certainly emitting one tool_use per turn instead of batching.

**Anti-patterns (burn budget and time):**

- "One tool per turn" — model returns 1 tool_use, waits for tool_result, returns the next 1 tool_use. 8 independent `create_module` calls = 8 iterations instead of 1.
- "Ping-pong" — `create_X` → (next turn) `update_frontmatter(X)` → (next turn) `add_related_link(X)`. If you know upfront what you want, do it in 1 turn.
- "Granular perfectionism" — 13 tiny updates to the same note. Consider `replace_section` or `update_note`.

**If one parallel call fails validation** (e.g. bad argument schema): the other calls in the same batch still return their results. In the next turn, fix ONLY the failed call — do not re-issue the ones that succeeded (they already got an "ok" tool_result).

## What to base the plan on

1. **Collected chunk summaries** — these are your "analysis notes". Treat them as one coherent summary of the whole commit, grouped by files.
2. **Vault map** (MOCs + hubs + top-15 tags + sample stems per type, without full content) — to decide where to link and whether something already exists. Pull details via `list_notes` (with `include_preview=true` when you want a body snippet) / `read_note` (full content or selected `sections`) / `list_tags` (complete tag map when missing from top-15) / `vault_map` (MOC → hub → module hierarchy) / `find_related` (fuzzy by topic).
3. **Manual user changes in the vault** — if user already wrote something manually, don't overwrite; incorporate into the plan.
4. **Note type examples** (attached in the system prompt) — use their structure (frontmatter + sections) when creating new notes.

## Decision rules (reminder)

- One commit = one `changelog` note (via `create_changelog_entry`) — unless commit is trivial and you don't call any write tools.
- Architectural changes → `create_decision` (automatically adds a row to the ADR table in the parent hub).
- New code module → `create_module`.
- New concept in discussion → `create_concept`; new technology → `create_technology`.
- Generic `create_note` / `update_note` are **only** for `type: doc` (free-form docs). For types: `hub`, `concept`, `technology`, `decision`, `module`, `changelog` use the dedicated tools EXCLUSIVELY.
- Adding MOC entries — via `add_moc_link` (idempotent).
- Wikilinks `[[stem]]` instead of paths; frontmatter matching the examples.
- Orphan wikilink (you refer to something with no file) → `register_pending_concept`, don't block documentation.

## Session end format

At the end of the session, call `submit_plan` with fields:

- `summary`: 1-2 sentences on what you did for this commit and why (based on collected chunk summaries).

`submit_plan` no longer accepts an action list — those are already registered via individual tool calls. The summary goes straight into the user preview and into the vault commit message.

**An empty plan is allowed.** If the commit adds no documentation value (dep bump, formatting, trivial bugfix) — do NOT register any writes, just call `submit_plan(summary="...")` with the reason. Ideal path is 1 iteration.
