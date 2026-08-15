# /backlog — autonomous build work

Dit is de intake-queue voor Hermes' autonome build-agent (kanban board "code").
Add here all remaining build work / features that Hermes can implement autonomously.

## File format

One markdown file per user story, with YAML frontmatter:

```markdown
---
title: "Add CSV export for transactions"
status: todo        # todo | in-progress | done | cancelled
priority: 10        # higher = sooner (default 10)
---

## Context
Why this is needed.

## Acceptance criteria
- [ ] functional criterion
- [ ] test criterion
```

Write acceptance criteria specific enough that an agent can implement without
back-and-forth. `status: todo` = candidate for the next build task.

## Rules (how the pipeline works)

1. Hermes picks **one story at a time** (highest `priority` first) and creates
   one kanban goal-loop task for it. No flooding: at most 1 backlog story in
   flight at once.
2. The build agent implements it (with tests), opens a PR that also flips
   `status: todo -> done` in the same file, the PR is auto-merged when CI is
   green, and the Coolify deployment is verified.
3. The next story only starts when the previous one is released **without new
   incidents** (no new GlitchTip / Coolify / CI-failure issues for the repo).
4. Build agents run **only during DeepSeek off-peak hours**:
   off-peak = 00:00–01:00, 04:00–06:00 and 10:00–24:00 UTC
   (peak = 01:00–04:00 and 06:00–10:00 UTC).
5. Files named `README.md`, `_*.md` and `.*.md` are ignored by the sync.

To re-open a finished story: archive its kanban task (or rename the file) and
set `status: todo` again.
