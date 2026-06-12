# Health Dashboard Hardening and UX Implementation Plan

> **For Codex:** Use test-driven development and verification-before-completion for every implementation package.

**Goal:** Turn the current personal dashboard into a reliable, private, understandable health product without discarding the working data pipeline.

**Architecture:** Keep the Python-generated standalone dashboard and current sync integrations for compatibility. First secure the local HTTP boundary, make persistence resilient, sanitize all model-generated HTML, and remove the most visible information-density problems; then extract the monolith into domain, source, storage, AI, web, and UI modules behind the existing entry scripts.

**Tech Stack:** Python standard library, generated HTML/CSS/JavaScript, Chart.js, unittest, local JSON storage.

---

### Task 1: Protect the local API boundary

**Files:**
- Modify: `dashboard_server.py`
- Test: `tests/test_dashboard_server.py`

1. Add failing tests for same-origin validation, body-size limits, security headers, and hidden server tracebacks.
2. Run the focused server tests and verify the new tests fail for the intended reasons.
3. Remove wildcard CORS, reject cross-origin mutations, cap JSON request bodies, and return stable client errors.
4. Add `nosniff`, referrer, frame, and resource-policy response headers.
5. Run focused tests, then the full suite.

### Task 2: Sanitize model-generated UI content

**Files:**
- Modify: `build_dashboard.py`
- Test: `tests/test_dashboard_v2.py`

1. Add failing HTML-contract tests requiring a single allowlist sanitizer at every AI insertion point.
2. Verify the tests fail because cached analysis, symptom analysis, and chat replies currently reach `innerHTML` directly.
3. Implement a DOM-based allowlist that preserves only safe text and approved formatting tags.
4. Route modal, category, symptom, and chat AI output through the sanitizer.
5. Run focused tests, then the full suite.

### Task 3: Make local JSON writes atomic

**Files:**
- Create: `storage_utils.py`
- Modify: `dashboard_server.py`
- Test: `tests/test_storage_utils.py`

1. Add failing tests for atomic JSON replacement and cleanup after a failed replace.
2. Implement same-directory temporary writes, flush, fsync, and `os.replace`.
3. Use the helper for notes and body measurements first.
4. Extend it to sync and AI caches in a later package after route hardening is stable.

### Task 4: Fix the first UX hierarchy defects

**Files:**
- Modify: `build_dashboard.py`
- Test: `tests/test_dashboard_v2.py`

1. Add failing contract tests for concise category insight previews and non-emoji chat controls.
2. Render a short AI summary in section cards and leave the full answer in the modal.
3. Remove the clipped masked text behavior.
4. Replace remaining chat-control emoji with the existing SVG icon system and improve mobile spacing.
5. Verify sleep, nutrition, body, and mobile views in the in-app browser.

### Task 5: Publish the audit and staged roadmap

**Files:**
- Create: `docs/PROJECT_AUDIT_AND_ROADMAP.md`
- Modify: `README.md`

1. Document product strengths, risks, missing capabilities, and data-quality constraints.
2. Define P0-P3 priorities with acceptance criteria.
3. Describe the target module layout and compatibility migration sequence.
4. Add reproducible setup, privacy model, backup/restore, testing, and troubleshooting documentation.
5. Record verification evidence and remaining risks.

