# Atomic Entity + File Upload Plan

## Context

The Python `upload()` and `update_partially()` methods in `EntityRepository` currently send only JSON (`Content-Type: application/json`) to `POST /api/entities`. The `Entity` model carries `documents` and `attachments` lists but those contain only **metadata** (id, filename, originalname) — no actual file bytes. The `FileRepository` has separate `upload_file_from_bytes` / `upload_document_from_bytes` methods that upload files to `/api/files/upload/attachment` and `/api/files/upload/document`. This two-step flow creates a window where the entity is persisted but file uploads fail, leaving a corrupted entity in Uwazi.

Uwazi's `POST /api/entities` endpoint already supports atomic entity+file creation **in a single request** via `multipart/form-data`: `UploadMiddleware.multiple()` parses both the entity JSON and file bytes, and both `EntityFacade.create()` and `UpdateEntityUseCase` wrap entity+file operations inside `transactionManager.run()`. If file storage or DB insertion fails, the entire transaction rolls back. The Python client should always use multipart for this endpoint — no conditional needed, because `UploadMiddleware` gracefully handles no-file requests (empty `req.files` → `req.inputFiles` is `undefined` → downstream use cases skip file processing).

## Approach

### 1. Verify Uwazi backend atomicity (confirmed via `/home/gabo/ssd/projects/uwazi/app/api`)

- `POST /api/entities` with `multipart/form-data` — `UploadMiddleware.multiple()` populates `req.inputFiles` only when files are present (source: `UploadMiddleware.ts`)
- When `sharedId` absent → `EntityFacade.create(dto, language, inputFiles)` → `CreateEntityUseCase` wraps `entitiesService.insert()` + `fileService.insert()` in a single `transactionManager.run()` (source: `CreateEntity.ts` lines 35-50)
- When `sharedId` present → `UpdateEntityController` → `UpdateEntityUseCase` wraps `entitiesService.update()` + `fileService.insert/delete/bulkUpsert` in a single `transactionManager.run()` (source: `UpdateEntity.ts` lines 108-115)
- `UploadMiddleware.multiple()` classifies files as documents when fieldname matches `document` or `file`, otherwise as attachments (source: `UploadMiddleware.ts` `multiple()` method)
- When no files are uploaded, `req.inputFiles` is `undefined`, `CreateEntityUseCase` and `UpdateEntityUseCase` handle this gracefully with `input?.inputFiles?.filter(...)` and `(input.uploadedFiles || [])`

### 2. Modify `HttpClientAdapter` to support multipart POST requests

**File**: `uwazi_api/adapters/http_client_adapter.py`
- Currently hardcodes `Content-Type: application/json` and sends JSON via `data=json.dumps(payload)`. The underlying `requests.Session` natively supports `files` parameter for `multipart/form-data`.
- Modify the upload call path to use `request_adapter.post(url, files=..., cookies=...)` instead of `data=json.dumps(payload)` with explicit JSON content-type header.
- The `requests` library generates the correct `Content-Type: multipart/form-data` with boundary automatically when `files` is provided.
- Update `headers` to not override `Content-Type` when sending multipart, since `requests` sets it with the boundary automatically.

### 3. Modify `EntityRepository.upload()` to always use multipart

**File**: `uwazi_api/use_cases/repositories/entity_repository.py` lines 70-94
- Change `upload()` to always send `POST /api/entities` as multipart/form-data with the entity JSON in the `entity` form field.
- Remove the JSON `data=json.dumps(payload)` path entirely — multipart is used for all requests, with or without file bytes.
- When files are present (passed as an optional `files` parameter), add them as multipart fields alongside the `entity` field.
- When no files are present, send just the `entity` field — Uwazi handles this identically (no `inputFiles` → no file processing).
- Use the multipart body construction pattern from `file_repository.py`'s `_build_multipart_body` (lines 9-53) as reference, adapted for the entity JSON in the `entity` field and multiple file fields.

### 4. `update_partially()` inherits the fix automatically

**File**: `uwazi_api/use_cases/repositories/entity_repository.py` line 120
- `update_partially()` delegates to `self.upload(merged_entity, language)` at line 120, so it always uses multipart now.
- The Uwazi `UpdateEntityController` routes `POST /api/entities` with `sharedId` present to the update path (source: `routes.js` line 83), which also passes `req.inputFiles` through to `UpdateEntityUseCase` with transactional guarantees.

### 5. Decide how file bytes are associated with the Entity model (Option B)

Keep `Entity` metadata-only and add a separate `files` parameter to `upload()` and `update_partially()`: `upload(entity, language, files=None)` where `files` is a list of `{fieldname, filename, content, content_type, originalname}` dicts. This avoids polluting the domain model with transport-layer data.

### 6. Reuse existing multipart body builder pattern

**File**: `uwazi_api/use_cases/repositories/file_repository.py` lines 9-53
- `_build_multipart_body(entity_id, title, file_bytes, content_type)` builds the correct multipart format with `entity`, `originalname`, and `file` fields.
- For entity uploads, adapt this pattern: use the entity JSON as the `entity` field instead of just the `entity_id`, and allow multiple `file`/`document` fields.

## Critical files & anchors

- `uwazi_api/adapters/http_client_adapter.py` — needs multipart POST support; currently hardcodes `Content-Type: application/json`
- `uwazi_api/use_cases/repositories/entity_repository.py` lines 70-120 — `upload()` and `update_partially()` should always use multipart/form-data
- `uwazi_api/use_cases/repositories/file_repository.py` lines 8-53 — `_build_multipart_body` is the reference pattern for multipart construction
- `/home/gabo/ssd/projects/uwazi/app/api/entities/routes.js` lines 75-100 — Uwazi backend route that handles atomic create/update with `req.inputFiles`
- `/home/gabo/ssd/projects/uwazi/app/api/core/application/CreateEntity.ts` lines 35-50 — transactional entity+file insert in Uwazi
- `/home/gabo/ssd/projects/uwazi/app/api/core/application/UpdateEntity.ts` lines 108-115 — transactional entity+file update in Uwazi
- `/home/gabo/ssd/projects/uwazi/app/api/core/infrastructure/express/middlewares/ UploadMiddleware.ts` — `UploadMiddleware.multiple()` parses multipart into `req.inputFiles` (no-op when no files present)
- `/home/gabo/ssd/projects/uwazi/app/api/core/infrastructure/express/entity/UpdateEntityController.ts` — passes `inputFiles` to mapper for atomic updates
- `/home/gabo/ssd/projects/uwazi/app/api/core/infrastructure/facades/EntitiesFacade.ts` — create/update facades accepting `inputFiles`

## Verification

1. **Unit test**: Verify that `upload()` constructs a multipart body containing `entity` JSON field, and when files are passed, includes `file` binary fields with correct content_type and `originalname` fields.
2. **Integration test**: Create an entity with a PDF via `upload()` in a single call, verify the entity exists in Uwazi with the document attached.
3. **Atomicity test**: Verify that if file upload fails (e.g., invalid bytes), the entity is NOT created — confirm by checking Uwazi for the entity's absence.
4. **Update atomicity test**: Call `update_partially()` with a `shared_id` and new file bytes, verify the entity is updated atomically (both metadata and new document).
5. **Regression test**: Verify that `upload()` with no file bytes still works via multipart (no files → entity created without documents).
6. **No-conditional test**: Confirm there is no `if files: ... else: json` branch in `upload()` — it always uses multipart.

Run: `python -m pytest uwazi_api/tests/ -v -k "upload or update_partially"` (after creating tests per AGENTS.md isolated unit testing rules).

## Assumptions & contingencies

- Uwazi's `POST /api/entities` with `multipart/form-data` is the endpoint for both atomic creation and update. There is no separate endpoint that provides stronger guarantees.
- The `UpdateEntityController` at `POST /api/entities` with `sharedId` present also handles files atomically, as confirmed by reading its source.
- If Uwazi's `UploadMiddleware.multiple()` changes the fieldname convention, the Python multipart builder would need updating. Reference `_build_multipart_body` in `file_repository.py` for the stable field mapping.
- The Python client's `HttpClientAdapter` uses `requests.Session` which natively supports multipart via the `files` parameter — no new dependency needed.
- File bytes associated with `Entity` documents/attachments arrive from the agent mapper layer; current `_build_api_entity` in `entity_mapper.py` does not pass file bytes, so the mapper would need a corresponding update to carry file content through.
- Always using multipart means JSON-only requests are no longer supported; this is a deliberate simplified trade-off since the `requests` library handles both multipart encoding correctly.
