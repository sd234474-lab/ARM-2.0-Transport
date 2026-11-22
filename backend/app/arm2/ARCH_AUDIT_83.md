# Issue 83 — common ARM2 infrastructure

Integration base: `0ee17241f80222b8ff55eadb274e904aa6f50acb`, already deployed R2,
after PR82 document preview and compact card changes. Schema head remains 0071.
No preview implementation, stored files, subject rules or core SAVE adapters are
rewritten. `Documents.tsx` changes only the guard import/removal.

| Finding | Before | Common mechanism / actual consumers | Verification / after |
| --- | --- | --- | --- |
| A — CONFIRMED FIX | Three mounted-form navigation implementations | `frontend/src/arm2/useDraftGuard.ts`; DOCUMENT, DOC-KIND, Workspace owner selection | Push/replace/go, beforeunload, native POP cancellation/restoration, listener cleanup; existing DOC-KIND browser back/forward/parked-draft and owner/document suites. One implementation remains. |
| B — CONFIRMED FIX | Two copied APIRoute body collectors | `http_limits.bounded_route`; B4 bulk (5 MiB), B5 questionnaire (32 MiB) | Actual configured limits -1/exact/+1 byte, streamed chunks, false Content-Length, empty/tiny chunks, rejection before reading tail. Same CSRF/auth middleware and operation dependencies remain. |
| C — CONFIRMED FIX, bounded common part | Repeated controlled-exception status mapping | `http_errors.controlled_error` and safe `rollback`; routes `_save`, B3 `translated`, B5 `translated` | 403/404/409/422, SQL redaction, failed rollback, commit ownership, storage 503 and B5 structured fallback. Generic SQL errors in `_save` now get explicit rollback and the same generic HTTP 500 outcome. |
| C — NO CHANGE, process policies | Different commit / uncertain SQL / storage handling | `_save` still commits once; B3/B5 operations still own their transactions | B3 uncertain SQL remains 503 with same-key retry. B5 integrity/SQL responses remain 409; its storage fallback remains structured 503. No automatic operation retry or new commit in shared code. |
| D — REQUIRES ARCH DECISION | Two supported presets; no `file_naming_fields` | Existing single `document_file_storage.safe_name` remains authoritative for B3/B5 | DOC-036 08 rows 81–82 and DOC.FILE.NAME.R01/R02 require registered parameters but do not specify the complete selectable field/template allowlist or missing-value rule. Requested owner's exact approved settings; no invented field selection, date algorithm or migration. |

## D decision needed

Specify the exact additional template codes and registered sources, which sources
are permitted for CP/ORG/VEH, required/optional parameters and behavior when a
selected value is absent; confirm the compatible schema/API migration for
`file_naming_fields`. Existing two presets and current values must remain valid.
Until that decision, no new date fields, procedural dates, arbitrary formulas,
new field storage or changes to stored/original/exported names are introduced.
This PR does not claim D is implemented or the entire platform is certified.

## Preservation and rollout

No migration/reset, business data cleanup, rights adjustment or file reupload.
Regression uses a new local disposable `arm2_issue83_*_test` database; never R2/B1.
Ubuntu rollout requires exact old HEAD, existing R2 project/DB/volume/port guards,
private pre-update backup and preserved environment fingerprint (except source
SHA). Build actual API and web images; do not retag the old API after backend
changes. Recreate only R2 API/web/gateway, never DB or volumes. Verify schema 0071,
health, old B1/DB IDs, unchanged mounts/credentials and protected browser access.
No merge. Owner authorized this R2 update in the Issue 83 task.
