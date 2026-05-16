---
tags: [request, resolved, high]
project: [[projects/Dream Calendar]]
status: resolved
severity: high
value_score: 5
updated: 2026-05-16 14:24
---

# GET /api/events/today returns 500 — calls list_events() directly, bypassing Query() defaults

✅ **Resolved**  ·  severity: `high`  ·  value: `5/10`  ·  priority score: `15`

**Project:** [[Dream Calendar]]
**Source:** `internal`
**Task:** [[73863fa1]]

## Description

WHAT: curl http://127.0.0.1:5041/api/events/today returns HTTP 500 'Internal Server Error'.

HOW: app.py:392 calls `return list_events(date=_today_iso())`. list_events declares from_date / to / project_id with FastAPI Query() defaults. Calling list_events as a plain function (not via the FastAPI router) leaves those parameters bound to Query objects rather than None — any subsequent `.startswith` / comparison / iteration on them blows up. Extract the body into a helper, e.g. _list_events(date, from_date, to, project_id) -> list, and have both /events and /events/today call the helper.

DONE WHEN: curl /api/events/today returns 200 + a JSON list (possibly empty); covered by a regression test in tests/test_dream_calendar.py.

*Auto-generated 2026-05-16 14:24*
