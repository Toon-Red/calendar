---
tags: [task, done, feature]
project: [[projects/Dream Calendar]]
status: done
priority: normal
updated: 2026-05-16 13:08
---

# Tomorrow's calendar still has 3 [FIX] events for tasks already marked done

✅ **Done**  ·  `feature`  ·  priority: `normal`

**Project:** [[Dream Calendar]]

## Description

WHAT: Tomorrow's calendar still has 3 [FIX] events for tasks already marked done
HOW: WHAT: tomorrow's calendar (2026-04-28) has 23 events; 3 of them point to PD tasks whose status is already 'done' (Calendar CRUD test x2, Create minecraft-server x1). EOD seeding ran clean today (12/12 tests pass) but the leftover events from the earlier failure round remain.

HOW: Same fix as request ebbaff3a — when a previously-failed integration test passes, auto-resolve the matching task AND delete its scheduled [FIX] event from upcoming calendar dates. Match by source_id == task_id.

DONE WHEN: tomorrow's calendar contains zero events whose source PD task is in 'done' status; verified via the audit script.
DONE WHEN:

*Auto-generated 2026-05-16 13:08*
