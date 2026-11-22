# ARM2 platform document mechanisms

DOC-035 and DOC-036 ARCH.CORE-01 / DOC.FILE.NAME-01: reuse common mechanisms;
processes retain their subject rules and supply context, not private renderers.

| Operation | Common implementation | Consumers |
| --- | --- | --- |
| Open independent preview | `filePreviewWindow.ts` | Questionnaire candidates/current files; DOCUMENT staged/current/historical files for registered parents |
| Authorised bounded byte retrieval | `FilePreviewPage.tsx`, `api.ts:previewBytes` | All preview sources |
| PDF/image rendering and original download | `FilePreview.tsx` | One common preview page |
| Safe naming | backend ARM2 `document_file_storage.safe_name` | Ordinary upload, questionnaire import, download |
| Disk storage and verified originals | backend ARM2 document file storage modules | Ordinary upload and questionnaire import |
| Atomic revision publication | backend ARM2 `publish_prepared_revision_in_transaction` | Ordinary acceptance and questionnaire acceptance |

The existing `questionnaire-preview.html` URL is retained as a compatibility
entry, not a questionnaire-specific renderer. It now mounts `FilePreviewPage`.
Only whitelisted ARM2 endpoints are accepted. Server authorization, MIME/content
validation, SHA checks and download rules remain authoritative and unchanged.
PDF uses the pinned local worker and canvas only, with annotations/XFA disabled;
opaque originals are never displayed inline. No provider URLs or tokens go to
the preview page. The original file is not changed by viewing it.

For staging, reserve the tab synchronously in the click handler, then navigate
after staging succeeds; close it on failure. Browsing never accepts a revision.
New consumers use this contract; changes are tested against existing consumers.
No host Metka application code, ARM1, database schema or stored data is modified
by this frontend refactoring.
