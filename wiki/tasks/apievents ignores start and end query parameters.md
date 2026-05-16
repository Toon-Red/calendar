---
tags: [task, done, feature]
project: [[projects/Dream Calendar]]
status: done
priority: high
updated: 2026-05-16 13:08
---

# /api/events ignores start and end query parameters

✅ **Done**  ·  `feature`  ·  priority: `high`

**Project:** [[Dream Calendar]]

## Description

WHAT: /api/events ignores start and end query parameters
HOW: As a UI consumer of /api/events, I want to fetch only the events in my visible window so I'm not pulling 1678 records per render.

Expected: GET /api/events?start=2026-04-27&end=2026-04-28 returns just that day.
Actual: Same call returns the full 1678-event payload regardless of start/end.

Reproduce: fetch('http://127.0.0.1:5041/api/events?start=2026-04-27&end=2026-04-28').then(r=>r.json()).then(d=>d.length) -> 1678.
DONE WHEN:

*Auto-generated 2026-05-16 13:08*
