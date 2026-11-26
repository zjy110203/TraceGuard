
## 2026-11-26 F6-A
Verified clean bb8d97f branch and origin; preserved all prior commits. Formal reads moved under shared single-process case locks, digest CAS and immutable target refusal; identical proposals cannot adopt files. Added real threaded races and idempotency assertions. Evidence: .traceguard-tmp/r5/f6-final/f6-a.log.

## 2026-11-26 F6-B / F6-C
Implemented durable INTENT -> COMMITTED -> DONE journal protocol, ownership-marked immutable formal files, fsync, fail-closed mismatch recovery, append-only torn-tail preservation and immutable rollback API/UI. Real Windows subprocess deaths cover intent/temp/replace/commit/state/recovery. Added HTTP races, audit/replay assertions and rollback write-failure retry. Final focused validation: 81 passed (f6-final-v2.log), plus 18 passed after stricter JSONL tail handling (f6-jsonl-final.log). Earlier failing logs retained. Supporting F7 work: real locator-failure evidence/diagnosis, controlled demo UI revisions, sandbox deployment binding and secret provider.
