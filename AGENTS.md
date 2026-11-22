# ARM2 engineering guidance — persistent architecture gate

**Authoritative sources (read selectively, not wholesale per task):**
- DOC-035 — platform architecture, working method, and verified-code handoff: https://docs.google.com/document/d/1KvIIBHI2miQe2mWqonZAF5-t5KtKfHx4dwHVo2NilfE/edit
- DOC-036 — actual approved requirements, object fields, rules, dependencies and technical acceptance gates: https://docs.google.com/spreadsheets/d/1jxj_ATB7pfgdbukaQ5mH2CdsPPisDAxU5ggitO2pPjI/edit
- DOC-037 — UAT observations and unresolved gaps (not a substitute for DOC-036): https://docs.google.com/spreadsheets/d/1R8HtDimUW6pER9kjLxyhAXDljMlaJrwUivBJqOVyEZo/edit

## Mandatory before implementing a change

1. Identify the **exact approved DOC-036 requirement IDs and H/K status** and the applicable DOC-035 platform contract; follow the **task's short, scoped requirements extract**. Open the linked source **only for unclear, missing or contradictory applicable clauses**. Do not rescan entire Google documents automatically and do not invent domain rules.
2. Verify actual integration base, SHA, migrations, dependencies, concurrent PRs, and exact consuming call sites. An Issue, a Draft PR, a passing CI, and a running older Ubuntu deployment are not proof of the same code.
3. Give the coordinator a concise map: **requirement → shared mechanism → consumers → domain parameters → executable acceptance test**. If the shared mechanism lacks capability, extend it **once** with regression for **all** consumers. Do not copy platform mechanisms into a screen, registry or import process.
4. Shared contracts include ID / SAVE / VERSION / HISTORY / ACCESS / VALREL and repeated file handling (protected access, preview, name calculation, upload, download, edition publication). Domain operations may validate *what*; shared services implement *how*. This does **not** authorize a giant generic rules engine.
5. Full acceptance: prove **the same real implementation is invoked** by all intended consumers, not merely that UI looks alike. Test before → operator action → system reaction → committed state → reopen, including permissions, no-op, conflicts, idempotency, history and preservation of unrelated users.
6. Distinguish **approved requirement / code present / integrated and tested / owner UAT accepted**. If a domain decision or verified base is missing, return a precise **STOP** and an actionable question; do not improvise a separate platform or silently change legacy data.

## Current high-priority example: VEH calibration

Approved `DOC-036 VEH.F08/F09, DISC-TM-05/08, VEH.R02` require a read-only confirmed body volume (m³) and confirmation date on VEH, an explicit operator verification of its document, and a separate customer approval keyed by **contract + license plate + confirmed volume**. Questionnaire volume is only a proposal; acceptance of a new DOCUMENT revision is not calibration confirmation or customer approval. Legacy prototype is evidence, **not** approved code to copy. See DOC-037 tab `17 Тарировка и согласование`; do not implement contract identity from unapproved assumptions.

**Scope/authority:** This file is guidance only, not a new business decision, not permission to deploy, merge, access secrets, or modify B1/production. Exact task scope and safe gating still govern.
