---
tags: [request, resolved, critical]
project: [[projects/Dream Calendar]]
status: resolved
severity: critical
value_score: 5
updated: 2026-05-16 14:24
---

# Dream Calendar unreachable during EOD UI playtest

✅ **Resolved**  ·  severity: `critical`  ·  value: `5/10`  ·  priority score: `20`

**Project:** [[Dream Calendar]]
**Requester:** eod_ui_playtest_2026-05-03
**Source:** `user_interview`
**Task:** [[0d5590fc]]

## Description

As a user expecting the EOD review to verify the ecosystem is healthy, I want a critical alert when a service won't respond so I can fix it before tomorrow.

Expected: GET Dream Calendar /api/health returns 200 OK.
Actual: connection failure — http://127.0.0.1:5041/api/health: <urlopen error [WinError 10061] No connection could be made because the target machine actively refused it>

Auto-detected by EOD UI playtest on 2026-05-03.

*Auto-generated 2026-05-16 14:24*
