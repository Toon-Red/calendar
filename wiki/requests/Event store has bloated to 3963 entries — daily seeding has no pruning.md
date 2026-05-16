---
tags: [request, resolved, normal]
project: [[projects/Dream Calendar]]
status: resolved
severity: normal
value_score: 5
updated: 2026-05-16 13:08
---

# Event store has bloated to 3963 entries — daily seeding has no pruning

✅ **Resolved**  ·  severity: `normal`  ·  value: `5/10`  ·  priority score: `10`

**Project:** [[Dream Calendar]]
**Source:** `internal`
**Task:** [[08d65305]]

## Description

WHAT: GET /api/events returns 3963 events. Each EOD seed adds ~20 events for tomorrow; completed-task notify writes also pile up. Most of the bulk is past-dated [FIX] events and 'task: …' history that no UI surfaces.

HOW: Either (a) add a retention policy: archive events older than 30 days into a separate file the live API doesn't read, with a /api/events/archive endpoint for occasional history queries; or (b) delete completed events older than 7 days outright. Add a one-shot `python -m calendar.compact` script to run from cron / Dream Auto.

DONE WHEN: live event count is bounded by recent activity; old completions live in an archive file; the GET /api/events response is consistently <500 KB.

*Auto-generated 2026-05-16 13:08*
