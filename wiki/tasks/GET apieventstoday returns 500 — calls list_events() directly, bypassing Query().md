---
tags: [task, done, feature]
project: [[projects/Dream Calendar]]
status: done
priority: high
updated: 2026-05-16 14:24
---

# GET /api/events/today returns 500 — calls list_events() directly, bypassing Query() defaults

✅ **Done**  ·  `feature`  ·  priority: `high`

**Project:** [[Dream Calendar]]

## Description

WHAT: GET /api/events/today returns 500 — calls list_events() directly, bypassing Query() defaults
HOW: WHAT: curl http://127.0.0.1:5041/api/events/today returns HTTP 500 'Internal Server Error'.

HOW: app.py:392 calls `return list_events(date=_today_iso())`. list_events declares from_date / to / project_id with FastAPI Query() defaults. Calling list_events as a plain function (not via the FastAPI router) leaves those parameters bound to Query objects rather than None — any subsequent `.startswith` / comparison / iteration on them blows up. Extract the body into a helper, e.g. _list_events(date, from_date, to, project_id) -> list, and have both /events and /events/today call the helper.

DONE WHEN: curl /api/events/today returns 200 + a JSON list (possibly empty); covered by a regression test in tests/test_dream_calendar.py.
DONE WHEN:

*Auto-generated 2026-05-16 14:24*
