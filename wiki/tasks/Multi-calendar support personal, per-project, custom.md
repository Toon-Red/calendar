---
tags: [task, done, feature]
project: [[projects/Calendar]]
status: done
priority: critical
updated: 2026-04-25 13:23
---

# Multi-calendar support: personal, per-project, custom

✅ **Done**  ·  `feature`  ·  priority: `critical`

**Project:** [[Calendar]]

## Description

WHAT: Calendar supports multiple named calendars. Each project gets its own automatically. User has a personal calendar. Custom calendars can be created.

HOW:
1. Data model: Calendar has_many Events. Each Calendar has: id, name, type (personal/project/custom), project_id (if type=project), color
2. Auto-create project calendars when Pipeline Dashboard projects are loaded
3. API: GET /api/calendars, GET /api/calendars/{id}/events, POST /api/calendars/{id}/events
4. Filter events by calendar, date range, project
5. Aggregate view: all calendars merged into one timeline

DONE WHEN: Each of the 17 Pipeline Dashboard projects has its own calendar. Personal calendar exists. Events can be created on any calendar. Aggregate view shows everything.

*Auto-generated 2026-04-25 13:23*
