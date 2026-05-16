---
tags: [request, resolved, high]
project: [[projects/Dream Calendar]]
status: resolved
severity: high
value_score: 5
updated: 2026-05-16 14:24
---

# /api/events ignores start and end query parameters

✅ **Resolved**  ·  severity: `high`  ·  value: `5/10`  ·  priority score: `15`

**Project:** [[Dream Calendar]]
**Requester:** ui_playtest_2026-04-27
**Source:** `user_interview`
**Task:** [[62ad36c7]]

## Description

As a UI consumer of /api/events, I want to fetch only the events in my visible window so I'm not pulling 1678 records per render.

Expected: GET /api/events?start=2026-04-27&end=2026-04-28 returns just that day.
Actual: Same call returns the full 1678-event payload regardless of start/end.

Reproduce: fetch('http://127.0.0.1:5041/api/events?start=2026-04-27&end=2026-04-28').then(r=>r.json()).then(d=>d.length) -> 1678.

*Auto-generated 2026-05-16 14:24*
