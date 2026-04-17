# Documentation plan finalization (FINALIZE)

You have already analyzed **all** diff fragments of this commit in prior conversation turns — you returned summaries of each chunk. Those summaries are collected below together with vault context and templates.

Now **finalize the work**: call the `submit_plan` tool EXACTLY ONCE with the action plan for the documentation vault.

## What to base the plan on

1. **Collected chunk summaries** — these are your "analysis notes". Treat them as one coherent summary of the whole commit, grouped by files.
2. **Vault state** (current notes, MOC, resources) — so you don't duplicate docs and can link to existing entries.
3. **Manual user changes in the vault** — if user already wrote something manually, don't overwrite; incorporate into the plan.
4. **Note templates** — use their structure (frontmatter + sections) when creating new notes.

## Decision rules (reminder)

- One commit = one `changelog` note (unless commit is trivial — empty `actions` list is fine then)
- Architectural changes → new `adr` note
- New code module → new `module` note
- MOC (`MOC__*.md`) and index (`_index.md`) are **not** in the plan — the agent planner updates them itself
- Wikilinks `[[stem]]` instead of paths; frontmatter matching templates

## Response format

Call `submit_plan` with arguments:

- `summary`: 1-2 sentences on what you're doing for this commit and why (based on collected summaries)
- `actions`: list of `AgentAction` with fields `type` (`create`/`update`/`append`), `path` (relative, `.md`), `content` (full body for create/update, appendix only for append)

Do not write anything outside the tool call.
