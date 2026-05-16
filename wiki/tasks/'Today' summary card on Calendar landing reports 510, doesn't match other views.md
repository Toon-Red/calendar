---
tags: [task, done, feature]
project: [[projects/Dream Calendar]]
status: done
priority: normal
updated: 2026-05-16 13:08
---

# 'Today' summary card on Calendar landing reports 510, doesn't match other views

✅ **Done**  ·  `feature`  ·  priority: `normal`

**Project:** [[Dream Calendar]]

## Description

WHAT: 'Today' summary card on Calendar landing reports 510, doesn't match other views
HOW: As a user, I want all 'today' counts derived the same way so they agree.

Expected: Calendar landing 'today' = number of events overlapping today's local date.
Actual: Landing card says 510. Filtering /api/events client-side by new Date().toDateString() yields 242. Dream standup says 403.

Likely cause: TZ + multi-day-event counting differs across components.
DONE WHEN:

*Auto-generated 2026-05-16 13:08*
