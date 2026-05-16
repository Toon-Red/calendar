---
tags: [task, done, feature]
project: [[projects/Dream Calendar]]
status: done
priority: high
updated: 2026-05-16 13:08
---

# Integration test failed: Calendar: list calendars

✅ **Done**  ·  `feature`  ·  priority: `high`

**Project:** [[Dream Calendar]]

## Description

WHAT: Integration test failed: Calendar: list calendars
HOW: WHAT: The Dream EOD integration suite test 'Calendar: list calendars' failed.

HOW: Service 'calendar' returned an unexpected response. Reproduce by running `python -c "from tool_tests import calendar; ..."` or by hitting the relevant endpoint manually.

DONE WHEN: The test passes on the next EOD run.

Error detail: URLError: <urlopen error [WinError 10048] Only one usage of each socket address (protocol/network address/port) is normally permitted>
Severity: high (per SERVICE_SEVERITY)
Duration: 0ms
DONE WHEN:

*Auto-generated 2026-05-16 13:08*
