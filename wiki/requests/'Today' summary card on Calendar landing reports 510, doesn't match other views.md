---
tags: [request, assigned, normal]
project: [[projects/Dream Calendar]]
status: assigned
severity: normal
value_score: 5
updated: 2026-05-16 13:08
---

# 'Today' summary card on Calendar landing reports 510, doesn't match other views

❓ **Assigned**  ·  severity: `normal`  ·  value: `5/10`  ·  priority score: `10`

**Project:** [[Dream Calendar]]
**Requester:** ui_playtest_2026-04-27
**Source:** `user_interview`
**Task:** [[248871a9]]

## Description

As a user, I want all 'today' counts derived the same way so they agree.

Expected: Calendar landing 'today' = number of events overlapping today's local date.
Actual: Landing card says 510. Filtering /api/events client-side by new Date().toDateString() yields 242. Dream standup says 403.

Likely cause: TZ + multi-day-event counting differs across components.

*Auto-generated 2026-05-16 13:08*
