---
tags: [request, assigned, high]
project: [[projects/Dream Calendar]]
status: assigned
severity: high
value_score: 8
updated: 2026-05-16 14:24
---

# Integration test failed: Calendar: create+read+delete event

❓ **Assigned**  ·  severity: `high`  ·  value: `8/10`  ·  priority score: `24`

**Project:** [[Dream Calendar]]
**Source:** `tool_tests`
**Task:** [[ee7e0dd7]]

## Description

WHAT: The Dream EOD integration suite test 'Calendar: create+read+delete event' failed.

HOW: Service 'calendar' returned an unexpected response. Reproduce by running `python -c "from tool_tests import calendar; ..."` or by hitting the relevant endpoint manually.

DONE WHEN: The test passes on the next EOD run.

Error detail: AssertionError: cleanup delete returned 500 (probe event e936c4ab may leak)
Severity: high (per SERVICE_SEVERITY)
Duration: 99ms

*Auto-generated 2026-05-16 14:24*
