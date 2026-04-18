# Task: audit and fill the vault MOC

**Project:** {{project_name}}
**Vault:** `{{vault_path}}`
**Main MOC:** `{{moc_path}}`
**Language:** {{language}}

---

## Context

{{trigger_context}}

---

## What to do

1. **Start with `moc_audit`** — without it you know nothing about vault state.
2. Based on the report decide which gaps are **actually worth acting on**
   (see priorities in system prompt).
3. Create hubs/technologies/concepts via `create_hub` / `create_technology`
   / `create_concept`. Every new note **must have concrete content** —
   don't create empty skeletons.
4. Link new notes under proper MOC sections via `add_moc_link`
   (`Hubs`, `Technologies`, `Concepts`, `Architectural decisions`).
5. Cross-link new notes from related modules via `add_related_link`
   (especially hubs from "their" modules, technologies from modules using them).
6. At the end call `moc_set_intro` with a short (2-4 paragraphs) MOC intro:
   what this vault is, how to read it, where to find what.
7. Close session with `submit_plan`.

If the audit shows everything is fine (no hub suggestions, zero orphans,
sections populated, intro exists) — **return
`submit_plan(summary="MOC audit clean — no changes.")` on the first
iteration**.
