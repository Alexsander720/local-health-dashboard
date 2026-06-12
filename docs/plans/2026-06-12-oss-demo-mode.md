# OSS Demo Mode Implementation Plan

**Goal:** Add a deterministic public demo that exercises every dashboard section without reading or exposing private health files.

**Architecture:** A dedicated `health_dashboard.demo_data` module owns synthetic health data, measurements, food profile, and cached insights. `build_dashboard.py` accepts a `demo_mode` flag and `dashboard_server.py --demo` serves the same read-only dataset while rejecting mutations.

**Tech Stack:** Python 3.10+, standard library, existing standalone HTML/CSS/JavaScript renderer, `unittest`.

---

### Task 1: Synthetic Dataset

**Files:**
- Create: `health_dashboard/demo_data.py`
- Create: `tests/test_demo_mode.py`

1. Write tests requiring deterministic data for sleep, body, nutrition, activity, health, notes, and workouts.
2. Verify the tests fail because the module does not exist.
3. Implement the smallest deterministic generator satisfying the data contract.
4. Verify the focused tests pass.

### Task 2: Renderer Isolation

**Files:**
- Modify: `build_dashboard.py`
- Test: `tests/test_demo_mode.py`

1. Add a failing test that demo rendering contains the demo identity and never embeds the private identity.
2. Add `demo_mode` to `render_html()` and `build()`.
3. Route demo measurements, food profile, and AI cache through synthetic providers.
4. Add `python build_dashboard.py --demo`.
5. Verify focused renderer tests pass.

### Task 3: Read-Only Demo Server

**Files:**
- Modify: `dashboard_server.py`
- Test: `tests/test_dashboard_server.py`

1. Add failing tests for demo status and mutation rejection.
2. Add `--demo`, serve synthetic `/api/data`, and reject all POST mutations.
3. Verify server tests pass.

### Task 4: OSS Documentation And Verification

**Files:**
- Modify: `README.md`
- Modify: `docs/PROJECT_AUDIT_AND_ROADMAP.md`

1. Document one-command demo startup and privacy guarantees.
2. Build demo HTML.
3. Run the complete test suite.
4. Verify desktop and mobile layouts in the browser.
