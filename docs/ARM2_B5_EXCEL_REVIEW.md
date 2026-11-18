# B5 Excel exchange review (2026-11-18)

Starting commit: `7a4ae66a74b45efe88b9098b6225b749ba0fc282`, branch
`codex/arm2-b5-questionnaire`, Draft PR #71. Frozen B1–B4 parent remains
`ddadebd7be7f5044628fd6b2ee656ca69d823db7`.

Sources: supplied legacy ARM_IMPORT_V2 workbook and filled ZIP sample, supplied
target workbook project, read-only DOC-036 «03 Конфигурация» (DISC-QI-01–11,
DISC-CP-25–39, DISC-TM-03, VEH.F01–F09). The project workbook is a field/layout
reference, not authority to add domain fields or authorization operations.

| Requirement | Existing WIP implementation | Gap | Correction |
| --- | --- | --- | --- |
| Legacy V2 without REP sheet/new IDs | Passport B3–B30, VEH B–V, formula caches, plate matching | No supplied-sample structural regression; inert email hyperlink rejected | Added synthetic sample structure; discard ordinary worksheet hyperlinks without navigation |
| Bank strings and caches | Text storage, length validation | Numeric 20-digit Excel accounts may already be rounded | Block selection of numeric accounts pending text clarification; preserve exact text zeros; read cached passport date |
| CP candidate | INN/name candidates; explicit operator choice | CP hint was only displayed | Include matching live CP ID/name as candidates without automatic selection |
| All representatives, including archived | Passport signer only | Full representatives sheet absent | Full V3 sheet, contacts, archive/restore proposals, blank rows, no deletion from omissions |
| Single signer, two projections | CP primary_signer_id and signed export identity | No cross-sheet comparison/conflict decision | ID-based shared proposal; explicit per-field passport/list source; separate field approval reset when source changes |
| Current access information | No access mutation from workbook | Export lacks actual linked-user/access information | Export current account state and effective ARM permissions; ignore incoming access/user columns |
| Permanent CP/REP/VEH IDs | CP/VEH and protected FILE manifest | REP ID only in manifest | Visible REP IDs, strict CP ownership, reject duplicate IDs and creating a new contact from an existing ID |
| VEH catalog and row modes | Full hidden catalog, archived/unowned VEH, lookup formulas | Missing complete target validations/status lists | Hidden «Справочники», full current catalog, dynamic VEH/CONFIRMED TYPE lists, three row modes, formula caches and runtime rechecks |
| XLSX/ZIP/documents | Current originals, signed parent/DOCUMENT/revision/FILE/SHA mapping; missing-file confirmation | Need target-format round-trip regression | Existing document tests now exercise V3 exports; enforce expanded OOXML + attachment size, not compressed XLSX size alone |
| Format version | Export says ARM_IMPORT_V2 | New structure needs a distinct format | Owner agreed ARM_IMPORT_V3 on 2026-11-18; export V3, recognize V2/V3 explicitly, reject project/unknown markers |

## V3 mapping and safeguards

Passport B3–B30 and VEH B–V retain their coordinates. The V3 representatives sheet
starts at row 5: B permanent CPREP-ID; C name; D position; E phone; F email;
G authority; H contact state; I current access; J linked user; K signer indicator;
L questionnaire note. I/J are ignored on import; K is a proposal hint, not a command
to assign a signer. L is shown during private review, not added to the CPREP domain
schema/history. The source project does not authorize a new persisted comment field.

All CP representatives, including archived records, are exported. Signer passport
cells are formula projections of the same representative row, with server-generated
caches for opening/importing without recalculation. Signed primary_signer_id and
row IDs bind the views, never equal names. Conflicting values require an explicit
source and field approval; changing the source invalidates its previous approval.
Missing sheets/rows/cells do not delete contacts, signer links, bank values or files.

IDs remain server-assigned. Existing-ID rows cannot create duplicate representatives;
unknown/foreign/duplicate REP IDs are review errors. Explicit operator matching is
required for unsigned new contacts, including legacy V2 signer data. No user account,
role, permission or user_id is created, assigned or changed by the workbook.

The workbook generator is isolated in `questionnaire_excel.py`. It is called from
the existing export function; document storage, protected file identity, draft and
atomic SAVE architecture are retained. Formulas reference only internal catalogs;
domain strings starting with `=` remain text. Excel diagnostics are informational;
server plate/type/owner/version checks remain authoritative. No volume/readiness or
customer approval operation is introduced.

## Legacy sample privacy and tests

`backend/tests/fixtures/arm_import_v2_anonymized.json` is a reconstructed **synthetic**
fixture, not a raw workbook export. All cell values, selector strings, V3 hints,
names, contacts, addresses, bank data, catalog entries and document filenames were
replaced. Only generic formula text, occupied-cell structure and number formats
were retained. Original ZIP attachments, workbook properties, comments, drawings,
personal data and source filenames are not included. The 12 data rows and 386
formula cells reproduce the legacy multi-sheet/cache behavior. A synthetic mailto
hyperlink reproduces the inert relationship found in the filled sample. It is
stripped before loading; targets are never followed, returned or persisted. External
workbook relationships, macro/OLE content, XXE and non-inert hyperlink schemes remain
rejected. No security test for active external links was removed or weakened.

The tests construct an XLSX from this fixture and supply synthetic cached values.
They cover absence of the new REP sheet/IDs, V3 hints without treating them as ARM2
identity, passport mapping, B7 ignored, exact bank zeros, numeric-account refusal,
cached date, N/P vehicle binding and B28 CP binding. Invalid/unselected rows and
numeric accounts do not prevent accepting independently selected valid data.

## Author evidence

`docs/evidence/arm2-b5-exchange/excel-native.json` records a real Microsoft Excel 16
recalculation on a generated synthetic V3 workbook: changed VEH selector (including
an archived VEH), new/withdrawal modes, signer projection, named-range dropdown and
hidden catalog. The workbook saved by Excel was imported again: three VEH rows,
one signer, intact protected manifest and exact leading bank zeros. Evidence contains
only synthetic artifact hashes and technical outcomes. No real sample workbook,
attachment or Excel-generated personal metadata is committed.

Final local results: **94 B5 backend tests passed**, **443 frontend tests passed**,
Ruff on the entire backend passed, both frontend builds passed. Commands:

```text
uv run --project backend pytest backend/tests/test_arm2_questionnaire_exchange.py backend/tests/test_arm2_questionnaire_migration.py backend/tests/test_arm2_questionnaire_parse.py backend/tests/test_arm2_questionnaire_workflow.py -q --tb=short
uv run --project backend ruff check backend
npm test --prefix frontend
npm run build --prefix frontend
```

All 26 exchange tests are included in the 94 backend count. The final B5 run was
executed after the source-change approval reset and the strengthened legacy
plate-to-existing-VEH/document test. The broader ARM2 run passed 312 tests before
the last legacy-hyperlink correction; final GitHub CI runs the full backend suite
on Linux. CI is pending publication at the time this report is committed; check
the PR HEAD's Actions run. A local pass is not a GitHub CI result.

No new domain model, B1–B4 architecture, old migration, authorization matrix or
working database changes are part of this correction. The initial B5 migration
remains 0069 after 0068. This review and the legacy correction do **not** claim
completion of all remaining B5 work/T01–T33, operator acceptance or Ubuntu UAT.
