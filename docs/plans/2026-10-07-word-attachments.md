# Word Attachment Support Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Support local `.doc` and `.docx` attachments end-to-end while leaving PDF unsupported.

**Architecture:** Extract body/table text locally, then reuse FileService line-based reads and source-byte hashes. Classify Word files as TEXT (a text-readable attachment) so existing encrypted registration cards need no database migration. Re-extract on reads so source edits retain stable IDs and change detection. No Office process, macro execution, network conversion, or plaintext temporary files.

**Tech Stack:** Python, bounded ZIP/XML parsing with defusedxml, olefile for MS-DOC compound streams, existing PySide6 upload UI and pytest.

---

## Scope and alternatives

Prefer a small local reader over requiring installed Word/LibreOffice or uploading private documents to a conversion service. DOC supports Word 97–2003 binary piece tables (Unicode and compressed text); unsupported legacy/RTF/encrypted/corrupt files fail explicitly with resave guidance. DOCX supports body paragraphs and tables in document order, including explicit breaks and hyperlinks; it does not render page layout, OCR images, or follow external relationships. Line numbers refer to extracted text, not Word pages. Bound source and decompressed XML sizes and reject XML entities.

### Task 1: Reader contract and regression tests

**Files:** Create `tests/word_fixtures.py`, `tests/unit/test_word_documents.py`; update `tests/unit/test_file_service.py`.

1. Generate small deterministic DOCX packages and real compound-binary DOC fixtures in tests, including Unicode/table text and mixed piece encodings.
2. Add failing checks for classification, full and ranged reads, stable IDs/source hashes after edits, unsupported PDF, encrypted/corrupt/oversized inputs, and no registration on failed import.
3. Run `.venv/Scripts/python -m pytest tests/unit/test_word_documents.py tests/unit/test_file_service.py -q`; expect new cases to fail before implementation.

### Task 2: Bounded local extraction and FileService integration

**Files:** Create `src/limbowave/application/services/word_reader.py`; modify `src/limbowave/application/services/file_service.py`, `src/limbowave/domain/files.py`, `pyproject.toml`, `uv.lock`.

1. Implement format-signature validation, bounded ZIP/XML and OLE stream reads, body/table extraction, and actionable failures.
2. Add `.doc`/`.docx` to document suffix classification. Preserve source-byte size/hash/mtime, compute UTF-8 line offsets from extracted content, and wrap parsing errors as FileReadError.
3. Re-run focused tests; expect pass, including unchanged TXT/MD behavior.

### Task 3: Upload affordances and model-facing descriptions

**Files:** Modify `src/limbowave/app.py`, `src/limbowave/ui/chat_view.py`, `src/limbowave/ui/attachment_bar.py`, `src/limbowave/application/services/attachment_service.py`, `src/limbowave/application/services/tool_gateway.py`, `src/limbowave/infrastructure/extensions/policy_enforcement.ts`.

1. Share document/attachment filters and supported-format descriptions across pickers and tooltips.
2. State Word extraction scope and extracted-text line numbering in attachment notes without inlining content.
3. Confirm drag/drop and folder paths reuse classification and indexing without adding a separate import flow.

### Task 4: Integration and verification

**Files:** Extend existing attachment, gateway, persistence and UI tests as needed.

1. Verify Word text survives database reopening and reaches read_document by file_id without appearing inline in the prompt.
2. Run focused tests, `.venv/Scripts/python -m ruff check src tests`, `.venv/Scripts/python -m mypy`, then the feasible full test suite. Record unrelated failures separately.
3. Review the diff for accidental PDF support, new external services, raw binary reads, or changes to unrelated user work. Do not auto-commit.

## Verification notes

- Focused attachment/reader/gateway/Qt regression run: **137 passed** (includes 41 Word-specific cases).
- Changed Python files: Ruff passed. Full source type check: mypy passed (190 files). Dependency lock check passed.
- Independent compatibility smoke checks: Apache POI test-data/document fixtures `simple.doc`, `simple-table.doc`, `empty.doc`, `SampleDoc.doc`, and `SampleDoc.docx` all extracted successfully. The paired SampleDoc DOC/DOCX produced identical 137-character body text; the Word 97 table retained two rows and three columns. Downloaded fixtures were used only in ignored local test storage, not added to the repository.
- 500 deterministic corrupted-input mutations (DOC and DOCX) returned text or the expected actionable WordExtractionError, with no unwrapped parser exceptions. A discovered malformed-DEFLATE exception was fixed and given a dedicated regression test.
- Full-project regression is checked separately because this shared working tree also contains unrelated in-progress UI changes. Do not treat those changes as part of Word support.

- Full-project run completed: **2961 passed, 22 failed, 6 skipped, 2 teardown errors** in 884.54s. Failures were in existing UI rendering/navigation/animation/diagnostic tests, compression-marker ordering, and child-process output decoding; no Word/attachment regression failed. Full-suite log: `.var/word-attachments-pytest.log` (ignored). These failures were not changed as part of Word support.
- Additional attachment-context/history recovery checks: **27 passed**.
