# ARM 2.0 shared mechanisms

Follow DOC-035 and DOC-036 ARCH.CORE-01 and DOC.FILE.NAME-01.
Before adding a repeated system operation, identify its existing ARM2 implementation
and consumers. Extend that implementation instead of copying it into a directory or
process. Keep subject-specific matching and business rules in the subject module.

File preview callers use `filePreviewWindow.ts`; `FilePreviewPage` retrieves authenticated,
bounded bytes and `FilePreview` is the single PDF/image renderer. Never add another
PDF parser, iframe preview, arbitrary remote URL, or direct unauthenticated fetch.
Keep original-only formats download-only and preserve server authorization/security.

Naming, storage and publication already share the backend ARM2 document file modules:
`document_file_storage.py`, `document_file_store.py`, `document_file_routes.py`.
Reuse their contracts; preserve IDs, versions, history and original names.
Check affected consumers and failure cases when changing a shared mechanism.
Changes to ARM2 must not modify the host Metka application or ARM1.
