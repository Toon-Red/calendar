---
tags: [request, resolved, normal]
project: [[projects/Dream Calendar]]
status: resolved
severity: normal
value_score: 5
updated: 2026-05-16 14:24
---

# As a PM, I want per-project timeline views so I can track each project's velocity independently

✅ **Resolved**  ·  severity: `normal`  ·  value: `5/10`  ·  priority score: `10`

**Project:** [[Dream Calendar]]
**Source:** `internal`
**Task:** [[4fd73778]]

## Description

AS A: project manager monitoring the ecosystem

I WANT: Calendar to expose `/api/calendars/{cal_id}/timeline` showing all events from now to N days out, grouped by week, with per-event status (scheduled / completed / cancelled).

SO THAT: I can answer 'how is Dream tracking against its goal deadline' in one API call instead of joining events + tasks + goals manually.

WHAT: New endpoint that walks events on a calendar between today and today + N days (default 14), bucketed by ISO week, with summary counts.

HOW: Reuse `_filter_events()` from the today-fix; add weekly bucketing on the result.

DONE WHEN: GET /api/calendars/project-dream/timeline?days=14 returns {by_week: [{week_start, scheduled, completed, cancelled, events:[...]}]}.

*Auto-generated 2026-05-16 14:24*
