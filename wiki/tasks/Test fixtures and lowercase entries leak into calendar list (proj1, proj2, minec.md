---
tags: [task, in_progress, feature]
project: [[projects/Dream Calendar]]
status: in_progress
priority: normal
updated: 2026-05-16 14:24
---

# Test fixtures and lowercase entries leak into calendar list (proj1, proj2, minecraft-server)

🔵 **In Progress**  ·  `feature`  ·  priority: `normal`

**Project:** [[Dream Calendar]]

## Description

WHAT: Test fixtures and lowercase entries leak into calendar list (proj1, proj2, minecraft-server)
HOW: As a user, I don't want test data showing up alongside real calendars.

Expected: Only legitimate project calendars + personal.
Actual: project-proj1 / proj1, project-proj2 / proj2, project-minecraft-server / minecraft-server (lowercase - the real project's display name is 'Minecraft Server') all appear in the calendar list and as pills in Dream's calendar filter row.

Impact: Confusing UI; suggests writes to /api/calendars from tests aren't cleaned up.
DONE WHEN:

*Auto-generated 2026-05-16 14:24*
