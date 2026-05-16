---
tags: [request, resolved, high]
project: [[projects/Dream Calendar]]
status: resolved
severity: high
value_score: 8
updated: 2026-05-16 13:08
---

# Integration test failed: Calendar: list calendars

✅ **Resolved**  ·  severity: `high`  ·  value: `8/10`  ·  priority score: `24`

**Project:** [[Dream Calendar]]
**Source:** `tool_tests`
**Task:** [[6d02969c]]

## Description

WHAT: The Dream EOD integration suite test 'Calendar: list calendars' failed.

HOW: Service 'calendar' returned an unexpected response. Reproduce by running `python -c "from tool_tests import calendar; ..."` or by hitting the relevant endpoint manually.

DONE WHEN: The test passes on the next EOD run.

Error detail: URLError: <urlopen error [WinError 10048] Only one usage of each socket address (protocol/network address/port) is normally permitted>
Severity: high (per SERVICE_SEVERITY)
Duration: 0ms

*Auto-generated 2026-05-16 13:08*
