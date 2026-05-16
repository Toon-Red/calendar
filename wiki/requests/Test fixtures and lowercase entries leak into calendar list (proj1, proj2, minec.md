---
tags: [request, assigned, normal]
project: [[projects/Dream Calendar]]
status: assigned
severity: normal
value_score: 5
updated: 2026-05-16 13:08
---

# Test fixtures and lowercase entries leak into calendar list (proj1, proj2, minecraft-server)

❓ **Assigned**  ·  severity: `normal`  ·  value: `5/10`  ·  priority score: `10`

**Project:** [[Dream Calendar]]
**Requester:** ui_playtest_2026-04-27
**Source:** `user_interview`
**Task:** [[e2e8bb97]]

## Description

As a user, I don't want test data showing up alongside real calendars.

Expected: Only legitimate project calendars + personal.
Actual: project-proj1 / proj1, project-proj2 / proj2, project-minecraft-server / minecraft-server (lowercase - the real project's display name is 'Minecraft Server') all appear in the calendar list and as pills in Dream's calendar filter row.

Impact: Confusing UI; suggests writes to /api/calendars from tests aren't cleaned up.

*Auto-generated 2026-05-16 13:08*
