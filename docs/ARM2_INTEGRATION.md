# ARM2-INT-001 — B2 + B3 + TYPE + B4

All evidence below is **AUTHOR-RUN on synthetic disposable QA**, not UAT or independent acceptance.
The task is DOC-036 / «03 Конфигурация» / row 162. No DOC, REG, existing PR, protected
repository, installed application, production database or cloud provider was modified.

## Sources and assembly

New clone: `C:\ARM\arm2-integration-b2-b3-b4`.
New branch: `codex/arm2-integration-b2-b3-b4`.
Base: `codex/arm2-b2`, `60bb055e9224eb8db526ce704fca594be54e6085` (includes B3-B/C/D and B2).
Source: `codex/arm2-b4`, `c219111ec4fd2a0a8ffd70928e477dced7b567c7`.
Both remote HEADs were checked before coding, after the owner's separate authorization.

Applied source commits in order with `git cherry-pick --no-commit`:

1. `7aa1a78d808efe99bb8d3462331f6b00a4d21d89` — TYPE corrections and complete B4 VEH exchange.
2. `c219111ec4fd2a0a8ffd70928e477dced7b567c7` — cwd-independent Alembic test configuration.

No blanket ours/theirs resolution, history rewrite, merge or deploy. The integration PR
targets B2; the original PRs remain unchanged. Historical B4 documents/evidence are
carried over as source artifacts, not relabelled as integration evidence.

## The 13 overlapping files

| File | Combined behavior |
| --- | --- |
| `backend/app/arm2/object_registry.py` | B3 DOCUMENT/revision/archive fields plus TYPE command category |
| `backend/app/arm2/routes.py` | B3 move history, B2 owner endpoint, B4 router, TYPE response/update; static routers first |
| `backend/app/arm2/schemas.py` | B3 document contracts, B2 owner StrictBool, TYPE category/STS StrictBool |
| `backend/app/arm2/vehicle_store.py` | B2 fresh-principal/owner lock and TYPE category/assignment checks; populate_existing retained |
| `backend/tests/test_arm2_document_kind_migration.py` | Frozen legacy rows, existing IDs/history, protected rollback, integration head |
| `backend/tests/test_arm2_document_kind_rules_migration.py` | B3 migration boundaries and all assertions retained; head now 0068 |
| `backend/tests/test_arm2_document_migration.py` | Typed parent FK/history guards retained; head now 0068 |
| `frontend/src/arm2/SearchCatalog.tsx` | Owner action controls plus filtered B4 export/import |
| `frontend/src/arm2/Workspace.tsx` | B3 document UI, B2 owner context/guards, TYPE category and B4 route |
| `frontend/src/arm2/api.ts` | B3 FILE/revision/correction, B2 owner commands, TYPE category |
| `frontend/src/arm2/model.ts` | B3 labels plus TYPE category/completeness |
| `frontend/src/arm2/Workspace.test.tsx` | Existing B2/B3 assertions and B4 expectations |
| `frontend/src/arm2/arm2.css` | Both sets of UI classes |

[Provenance audit](evidence/arm2-integration/provenance.json) records all 13 intersections
and preservation of the files unique to either source (31 B2-only, 39 B4-only).
Comparison uses Git LF-normalized contents, so Windows checkout line endings do not
appear as functional changes. The only changes to B2-only files are migration-head
assertions in historical migration tests. B4-only runtime/config/parser/UI files match
their source. Renumbered migration files and their tests are explicitly identified.

`backend/alembic/env.py` adds bulk model registration without replacing B3 configuration;
`backend/app/config.py` retains common configuration and adds B4's table signing key.
All new B2/B3/B4 files are retained. No B5 package transaction was introduced.

## Migration line

```text
0064 → 0065_document_files (B3) → 0066_document_operations (B3)
     → 0067_type_plate_category (TYPE) → 0068_bulk_vehicle (B4, sole head)
```

B3 migration files are unchanged. TYPE's actor/correlation metadata is `migration-0067`;
its generic source remains `arm2-migration`. No B3 events are rewritten. TYPE data
transformation and B4 schema/guard logic are unchanged apart from migration numbering.
Historical migration tests retain their own B3 targets, restore checks, loss guards,
and assertions; only assertions about the complete integration head become `0068`.

No previous B4 database/snapshot was attached or relabelled. The integration PostgreSQL
was initialized afresh on loopback port 55450. Significant databases bearing old B4
`0065/0066` were not touched, stamped, or assumed to contain B3 schema. Such databases
need a separately approved migration plan before use with this branch.

## Joint scenarios and evidence

API scenarios are in `backend/tests/test_arm2_integration.py`. They use native login,
real PostgreSQL transactions and the existing file-backed B3 QA provider, with no live Disk.

| ID | Input → operator/API actions → observed results → DB/FILE/HIST → reopen |
| --- | --- |
| I01 | Confirmed car TYPE and VEH/document → B4 assign TYPE, B2 CP→ORG→unassign → exact owner versions, TYPE/category/VEH-ID unchanged → one VEH event per change, document bundle unchanged → GET TYPE/VEH/documents/history agrees |
| I02 | Two accepted B3 FILEs and two revisions on VEH → B4 update model, retry same command → one version increment → immutable FILE IDs/SHA and historical bytes, isolated DOCUMENT history, one matching HIST+receipt → reopened workspace SAVED and current VEH correct |
| I03 | CP-owned VEH and pending B4 table → B2 CP→ORG, submit stale B4 approval → 409; reopen clears stale approvals → old CP reassignment without separate confirmation rejected (422) → server owner remains ORG |
| I04 | Same VEH, confirmed TYPE, B4 assignment and B2 owner command → concurrent TYPE category edit/B4 apply/B2 update → only serialized success or 409/422 → no incompatible linked plate/category, owner valid, history count equals version → GET confirms committed state |
| I05 | Multi-file B3 document → RETYPE/MOVE, B2 owner change, CORRECT one file → old parent returns 404, corrected historical file cannot download (404), other file bytes remain → no spurious VEH events from document operations, exact document event count → reopened destination/history/download agree |
| I06 | New QA 0064 with legacy TYPE/VEH and independent B3-only 0066 DB with real FILEs → real upgrades; dump/restore B3 DB then upgrade → single head 0068 → nullable TYPE becomes REVIEW without inferred category; IDs/links/VEH versions and B3 rows/events/files preserved; FKs validated; protected downgrade retained → API/historical bytes verified after restore |
| I07 | Two logged-in sessions, same VEH/TYPE, another account → concurrent stale VEH/TYPE edits, repeated B4 command, no-op owner command, permission revocation → 409/403 and private-draft isolation → no duplicate history/receipt → both sessions reopen the committed version |
| I08 | Integrated VEH/TYPE/owner, current+historical FILEs, saved B4 draft+receipt → dump DB and separately copy file-backed provider; restore both into a different QA database/directory; restart PostgreSQL/backend/frontend → exact DB snapshot and file manifests equal → all FILE bytes downloaded, receipts verified, duplicate apply returns same receipt → native UI and API reopened |

I06/I08 runner: `backend/tools/arm2_integration_qa.py`.
Local server adapter: `backend/tools/arm2_qa_server.py`.
Native browser checks: `backend/tools/arm2_integration_browser.mjs`.
The runner creates fresh `arm2_int_*_test` databases only, does not restore over or
drop an existing DB, and can restart only this clone's own `.local/pg-data`.
It records the actual head/schema, hashes SQL dumps, copies and hashes FILE bytes,
compares all ARM2 tables/history and users, checks historical downloads and durable
receipts, then verifies a guarded downgrade has changed no rows. SQL backup alone
is insufficient for PASS.

Reproduce from the repository root with locked backend dependencies, PostgreSQL/PostGIS
tools, installed frontend dependencies, and a disposable local PostgreSQL account:

```powershell
# TEST_DATABASE_URL: local QA role/database ending _test; never a working database.
# ARM2_QA_PG_BIN: directory containing pg_dump, pg_restore and pg_ctl.
# ARM2_QA_PLAYWRIGHT: installed playwright package directory (for --browser).
# ARM2_QA_BROWSER_EXECUTABLE: optional installed browser executable.
$env:PYTHONPATH = "$PWD/backend"
uv run --project backend python backend/tools/arm2_integration_qa.py --restart-pg-data "$PWD/.local/pg-data" --browser
```

Generated databases/dumps, runtime files and credentials stay under ignored `.local`.
Published evidence contains only synthetic values, test results and hashes.

## Validation and handoff

Validation results and final restored UI evidence are recorded in
`docs/evidence/arm2-integration/`. The final commit SHA and Draft PR URL are recorded
in the handoff report/PR. Full backend regression, addressable ARM2 regression,
joint scenarios, frontend tests/build, Ruff and diff checks are run by the author.
Platform limitations in a Windows full run are reported explicitly and are not
masked by modifying unrelated tests or assertions. CI and independent integration
acceptance belong to the controller per task section 7; publication is not acceptance,
and the author does not wait for long CI. B5 remains outside this work.

Actual 18 November 2026 AUTHOR-RUN results:

- Existing ARM2 backend regression: **217 passed**.
- Joint I01–I05/I07 native authenticated API tests: **6 passed**, including stale TYPE
  and VEH versions, another account, permission revocation and concurrent writes.
- I06/I08 final runner: **PASS**, including a separate B3-only `0066` database,
  real dump/restore, **all validated FKs**, protected downgrade, exact DB/FILE/HIST
  comparison, restarted PostgreSQL/backend/frontend and native browser verification.
- Frontend: **65 files / 435 tests passed**; main and citizens builds passed.
- Ruff and `git diff --check`: **PASS**.
- Full backend command: **1296 passed, 17 failed, 13 skipped, 2 errors**, coverage **92%**,
  1327 collected, 1007.36 seconds. This local Windows run is **not green**. All 17 failed
  node IDs were also present in the previous local `c219111e` author run, and their
  test files are unchanged from that source: 12 exchange reuse tests, one ML test,
  three Windows symlink privilege tests, and one POSIX file-mode test. Both errors
  are setup/teardown of the same existing image parameter whose name exceeds the
  Windows environment-variable length limit. No ARM2 test failed. Existing skips
  were not introduced or expanded (12 Tesseract-dependent cases and one integration
  prerequisite). The full failed-node list and source comparison are published.

The author environment is Windows / Python 3.13.16 / PostgreSQL 17.11 with ICU en-US;
GitHub CI uses Ubuntu / Python 3.12 / PostgreSQL 16. The isolated QA runtime reuses
already installed locked Python dependencies and verified OCR weights read-only;
code, databases, generated artifacts and branch changes belong to the new clone.
No claim of full Linux regression or independent acceptance is made from Windows results.

Published evidence:

- [Validation, all full-regression failures and baseline comparison](evidence/arm2-integration/validation.json).
- [I06/I08 exact restore/API observations, FILE/dump/snapshot hashes and migration audit](evidence/arm2-integration/restore.json).
- [Native browser assertions and historical FILE download hash](evidence/arm2-integration/browser-result.json).
- [Restored B4 workspace/receipt](evidence/arm2-integration/01-restored-bulk.png).
- [Restored VEH, TYPE and B2 owner](evidence/arm2-integration/02-restored-vehicle.png).
- [Restored current/historical B3 FILEs, identifiers and SHA](evidence/arm2-integration/03-restored-documents.png).
- [Restored TYPE category and completeness](evidence/arm2-integration/04-restored-type.png).

No new integration code/schema blocker was found. Independent Linux CI and controller
checks of the frozen integration SHA remain the handoff gate; no merge/deploy is authorized.
