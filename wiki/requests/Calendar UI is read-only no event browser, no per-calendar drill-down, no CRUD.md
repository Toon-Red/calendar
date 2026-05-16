---
tags: [request, assigned, normal]
project: [[projects/Dream Calendar]]
status: assigned
severity: normal
value_score: 5
updated: 2026-05-16 14:24
---

# Calendar UI is read-only: no event browser, no per-calendar drill-down, no CRUD

❓ **Assigned**  ·  severity: `normal`  ·  value: `5/10`  ·  priority score: `10`

**Project:** [[Dream Calendar]]
**Requester:** ui_playtest_2026-04-27
**Source:** `user_interview`
**Task:** [[ca7cb23e]]

## Description

As a user maintaining 21 calendars, I want a UI to browse events, add/edit/delete events, and rename/delete calendars.

Expected: Click a calendar row -> list of its events -> row actions (edit, delete).
Actual: Landing page is a static three-column table (id / name / type). No links, no buttons except 'Stop Claude'. Footer has API docs / Runs JSON / Health only.

Impact: Every event-level operation requires curl or hitting Swagger directly.

*Auto-generated 2026-05-16 14:24*
