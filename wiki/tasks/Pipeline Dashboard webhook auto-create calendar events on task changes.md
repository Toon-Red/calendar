---
tags: [task, done, feature]
project: [[projects/Calendar]]
status: done
priority: high
updated: 2026-04-25 20:59
---

# Pipeline Dashboard webhook: auto-create calendar events on task changes

✅ **Done**  ·  `feature`  ·  priority: `high`

**Project:** [[Calendar]]

## Description

WHAT: When tasks are created, started, completed, or deadlines set in Pipeline Dashboard, Calendar automatically receives events.

HOW:
1. Pipeline Dashboard fires webhooks on task state changes (or Calendar polls PD periodically)
2. Calendar creates/updates events: task-created, task-started, task-completed, deadline-approaching
3. Each event goes to the correct project calendar based on project_id
4. Goal deadlines from Dream also create Calendar events

DONE WHEN: Create a task in Pipeline Dashboard, see it appear on the project's calendar automatically. Complete it, see it marked done on the calendar.

*Auto-generated 2026-04-25 20:59*
