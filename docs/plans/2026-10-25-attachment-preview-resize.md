# Attachment image preview resizing

**Goal:** Resizing or maximizing an attachment preview scales the complete image with the available area, preserving aspect ratio and centered alignment.

**Design:** Keep the existing dialog, scroll container, and metadata. Add a resize-aware image label that retains the original pixmap, ignores pixmap size hints for layout, and renders to the available logical size at the display's pixel density. Preserve the existing initial screen-size limit. No zoom controls or attachment-storage changes.

**Alternatives:** `scaledContents` stretches images; scaling the already displayed pixmap loses detail. Scale from the retained original with `KeepAspectRatio` instead.

1. Add failing Qt regressions in `tests/ui/test_image_preview.py` for growing, shrinking, aspect ratio, small originals, high-DPI sources, maximize/restore, and repeated resize quality.
2. Update `src/limbowave/ui/sent_attachments.py` with the resize-aware label and use logical image dimensions for initial dialog sizing.
3. Run the preview tests, adjacent attachment/chat tests, lint, and typing. Render a preview at two sizes for visual verification.

## Validation

- Before the fix, all 10 preview regressions failed: images stayed at their opening size or forced scrollbars instead of fitting smaller viewports.
- After the fix, preview, attachment bar, chat view, and message motion tests: 116 passed.
- Preview tests rerun at 150% and 200% display scale: 10 passed at each scale.
- Focused Ruff lint/format and mypy checks passed.
- Inspected offscreen renders with the application stylesheet at 360 × 280 and 760 × 560: the full image stays centered and proportional, including all four corner markers and the metadata row.

## Follow-up: preserve the actual original resolution

The resize tests originally supplied full-resolution pixmaps directly and missed an upstream 256 × 256 downscale in `app._read_sent_attachment`. Stored/sent bytes were unchanged, but both imported and historical preview data used a low-resolution copy.

Remove that eager downscale while keeping worker-thread decoding, null-image handling, chip-local thumbnail rendering, and fit-to-window display. Verify through real application wiring for file imports, clipboard imports, and stored history; retain the existing original-byte storage/transmission tests.

### Resume verification (2026-10-25)

1. Recheck the existing no-downscale patch and the six real-import regressions.
2. Extend the wiring tests to cover stored messages loaded through conversation navigation and the lazy sent-attachment resolver, with no imported-preview cache to hide a regression.
3. Assert that decoded preview pixels, stored bytes, and outgoing image payloads match the original; keep chip-local thumbnails small.
4. Run the relevant UI/storage/payload suite at normal DPI, then the preview/import/history tests at 150% and 200%; check lint, formatting, typing and rendered previews.

Scope: preserve all unrelated working-tree changes. No new zoom controls, storage format changes, or changes to the screen-fit behavior.

### Follow-up validation

- The retained implementation only removes eager 256px scaling in `app._read_sent_attachment`; original-byte storage/transmission and chip-local thumbnails remain unchanged.
- Extended `tests/ui/test_attachment_resolution.py` to 12 end-to-end cases: file and clipboard imports, cold historical conversation loading, and the lazy sent-attachment resolver; landscape, portrait, and small images are covered.
- Checked exact preview pixel equality, original stored bytes, original outgoing payload bytes, and unchanged 40px/52px chip thumbnails.
- Reintroducing the old scaling in a separate Python interpreter (without touching shared source files) makes all 12 resolution cases fail. The actual source passes all 12.
- Preview and resolution tests at 150% display scale: 22 passed; at 200%: 22 passed.
- Focused Ruff lint/format, mypy for both production files, and whitespace checks passed.
- Visually inspected offscreen renders at 380 × 290 and 880 × 570 using a 1600 × 900 original, fine grid/line detail, and four corner markers. The image stays complete, centered and proportional. Renders are under `.tmp/attachment-preview-resume-20261025/`.
- Combined attachment, preview, chat, history, draft, send-queue, storage, payload and attachment-context regressions: **210 passed** (168.84 seconds).
