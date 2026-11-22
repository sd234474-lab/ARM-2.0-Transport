# ARM 2.0 platform principle

Apply DOC-035 and DOC-036 ARCH.CORE-01. Before implementing a repeated system
operation, identify the existing common mechanism and its consumers. Reuse it
or extend its contract; do not copy infrastructure into a directory or process.
Keep subject-specific validation and orchestration in their subject modules.
An independent mechanism requires evidence that reuse is unsuitable and an
explicitly agreed exception. Check all affected consumers after common changes.

Documents already share `document_file_storage`, `document_file_store` and
`document_file_routes` for naming, verified originals and revision publication.
Preserve server authorization, content/MIME/SHA checks, IDs, versions, history,
atomic saving, original names and download-only handling of opaque formats.
Questionnaire import must use these common services rather than private copies.

Use `http_limits.bounded_route` for bounded ARM2 request bodies, with the existing
consumer's byte limit and message. Use `http_errors.controlled_error` for common
controlled exception statuses. Transaction ownership and process-specific SQL,
storage and idempotency outcomes stay in the process; the mapper never commits.

Restrict ARM2 changes to ARM2. Do not edit ARM1 or the host Metka application's
other modules. Never commit secrets, private questionnaires, databases or `.local`.
