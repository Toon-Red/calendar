---
tags: [task, done, bug]
project: [[projects/Dream Calendar]]
status: done
priority: critical
complexity: M
updated: 2026-05-16 13:08
---

# Fix calendar startup failure -- won't spawn (Dream self-heal escalating)

✅ **Done**  ·  `bug`  ·  priority: `critical`  ·  `M`

**Project:** [[Dream Calendar]]

## Description

WHAT: dream/self_heal.py:ensure_services reports `Failed to spawn process (attempt 3)` for the calendar service every ~22 minutes when Dream Auto's EOD/morning runs fire. Surfaced via Discord escalation 2026-05-12 18:14.

WHY: Blocks EOD review from completing. Generates noisy Discord escalations on the schtasks cron cycle. Until calendar actually starts, the Dream automation registry (planned, see Dream's research items) will keep escalating.

HOW: Run `python C:/Users/prest/Desktop/code/calendar/app.py` directly. Capture the actual error -- likely an import path issue, a missing dep, a bad env var, or a database/file path that doesn't exist on this machine. Fix root cause; do NOT just silence the escalation.

DONE WHEN: `python calendar/app.py` launches without error AND listens on :5041 AND GET /api/health returns 200. Dream's self-heal stops escalating. (Discord goes quiet from this source.)

REFERENCE: 2026-05-12 investigation report; the Self-Heal Escalation Discord message originates at dream/orchestrator.py:1041 via dream/self_heal.py:ensure_services.

*Auto-generated 2026-05-16 13:08*
