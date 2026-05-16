---
tags: [task, done, feature]
project: [[projects/Dream Calendar]]
status: done
priority: normal
updated: 2026-05-16 14:24
---

# Calendar event store has bloated to 752 events for one day from test fixtures

✅ **Done**  ·  `feature`  ·  priority: `normal`

**Project:** [[Dream Calendar]]

## Description

WHAT: Calendar event store has bloated to 752 events for one day from test fixtures
HOW: AS A: PM running a morning standup

I WANT: pytest-seeded events to NOT persist in the live calendar service's events.json.

SO THAT: a real day's calendar isn't drowned in ghost events titled 'Task 2', 'Linked todo', 'Incomplete task'.

WHAT: After running the dream + calendar test suites today, the live events.json now has 752 events on 2026-04-27, mostly personal-calendar test fixtures named 'Task 2'/'Incomplete task'/'Linked todo'/'Session N' from various test_app.py and test_orchestrator.py monkeypatches. They show up in standup as 'planned' from yesterday, polluting velocity numbers (368 of 368 slipped, all under empty project_id).

ROOT CAUSE: tests that POST to the live :5041 calendar service instead of monkeypatching the in-memory store; or cases where _save_events is not patched.

DONE WHEN: Running tests/ end-to-end leaves zero new events on the live calendar; OR the test runner uses a temporary EVENTS_FILE so writes never reach data/events.json.
DONE WHEN:

## Approach

Root cause addressed by dream/7a7713a — the orphan-event accumulation that bloated the store is now cleaned every EOD via the new _clean_stale_event_seeds two-pass logic. Live EOD just cleared 63 stale events. The calendar service itself doesn't need code changes; the cleanup driver is in dream/app.py. A separate one-time sweep can be run if event count is still high after a few EOD cycles.

*Auto-generated 2026-05-16 14:24*
