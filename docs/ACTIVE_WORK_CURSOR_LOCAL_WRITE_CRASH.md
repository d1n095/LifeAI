# ACTIVE WORK — Cursor local write crash before audit/verify

**Branch:** `cursor/local-write-crash-before-verify`
**Base:** `e10ae97` (#181 merged)

Heal: on-disk content already equals requested after-hash → audit without rewrite.
Negative control: test fails when heal removed.
