# Health Dashboard: Audit and Roadmap

## Verdict

The project already has real product value: it combines sleep, body, nutrition,
activity, health metrics, notes, workouts, and AI analysis in one private local
dashboard. Its main limitation is no longer the visual style. The next quality
step is to make the data trustworthy, the interface selective, and the runtime
predictable.

The recommended strategy is evolutionary. Keep the working Python sync pipeline
and standalone dashboard, harden the current system, then extract modules behind
the existing commands. A framework rewrite would add migration risk without
solving the most important problems.

## What Is Strong

- Broad integration coverage across Mobvoi, Health Connect, Google Fit, YAZIO,
  KS Fit, measurements, and manual notes.
- Useful historical data rather than a decorative mock dashboard.
- A coherent modern visual system with section accents and shared SVG icons.
- Separate analytical screens for sleep, body, nutrition, activity, health,
  food profile, and notes.
- AI caches for global and category-level analysis.
- A functioning local server and automated refresh path.
- A growing regression suite that currently covers data merging, calculations,
  routing, and important HTML contracts.

## Main Product Problems

1. There is no true overview. Every section is detailed, but the product does
   not answer "what changed, what matters, and what should I do today?" in one
   place.
2. The KPI strip is now contextual to the active section, but the product still
   lacks a dedicated overview that prioritizes cross-domain changes.
3. The global AI banner and section AI card compete for attention.
4. Period controls look like dashboard filters but currently mostly control AI.
5. Data coverage differs by source, yet the UI rarely shows freshness,
   completeness, provenance, or inferred values.
6. Nutrition goals are absent, so energy balance cannot be computed honestly.
7. Sleep debt and regularity are now calculated from actual sleep time, but a
   broader recovery score still needs cross-domain inputs and uncertainty rules.
8. Long charts show too many dates and mixed scales without a clear default
   range or accessible tabular alternative.
9. The chat button can cover content on smaller screens.
10. Remaining emoji controls make parts of the UI look less consistent.

## Main Engineering Risks

### Privacy and security

- The local API previously emitted wildcard CORS and accepted cross-origin
  mutations. This is fixed in the first hardening package.
- Model output previously reached `innerHTML` directly. A DOM allowlist
  sanitizer now protects analysis, symptom results, and chat replies.
- `/api/data` exposes the complete local health dataset. Keep it same-origin and
  consider replacing it with purpose-specific endpoints.
- Chat history is stored in browser local storage. The UI needs a clear privacy
  notice, expiration option, and visible reset action.
- `gemini_key.txt` appears to be a legacy plaintext secret file while current
  code uses gcloud OAuth. Confirm it is obsolete, then remove it without reading
  or publishing its contents.

### Reliability

- Notes, measurements, sync datasets, AI caches, profiles, and archives now use
  atomic replacement.
- Sync and AI refresh now have single-flight job locks and observable job
  state. A true background task queue is still a later architectural step.
- The UI now exposes source counts and freshness. Persist structured per-source
  error history next instead of relying on broad exceptions and log text.
- `auto_refresh.log` grows without rotation.
- JSON files have no schema version or validation.
- Manual body measurements now validate their date-keyed schema and migrate
  legacy flat payloads instead of silently hiding or replacing history.
- External fonts and Chart.js require network access even though the product is
  presented as local.

### Maintainability

- `build_dashboard.py` combines calculations, content, CSS, JavaScript, and HTML
  in one very large module.
- Source adapters and normalization logic are concentrated in `health_sync.py`.
- Server routing, prompt assembly, persistence, and external AI calls share one
  process and broad exception handling.
- There is no reproducible project metadata, release process, or version control
  history yet.

### Health interpretation

- General prompts can phrase correlations and risk estimates too confidently.
- The product must distinguish observation, estimate, correlation, and medical
  diagnosis.
- Inferred sleep stages need a visible quality badge.
- Every AI conclusion should state the relevant date range and missing data.
- Red-flag symptom handling should remain separate from general wellness advice.

## Priority Roadmap

### P0: Trust and recoverability

- [x] Remove wildcard CORS and reject foreign-origin mutations.
- [x] Limit JSON request bodies and hide server tracebacks from clients.
- [x] Sanitize all model-generated HTML.
- [x] Make notes and measurements atomic.
- [x] Make all sync, profile, archive, and AI cache writes atomic.
- [x] Add a single-flight lock for sync and per-category AI generation.
- [x] Expose current per-source record counts and freshness in the API and UI.
- [ ] Persist per-source success, error, freshness, and last record timestamps.
- [ ] Rotate logs and cap retention.
- [ ] Remove or migrate obsolete plaintext secret files.
- [x] Initialize private version control before any public OSS work.

Acceptance: an interrupted write cannot destroy the last good dataset; duplicate
jobs cannot overlap; the UI can explain which sources are fresh or degraded.

### P1: Daily usefulness

- [ ] Add an Overview screen with three layers: today, trend, action.
- [x] Replace the global KPI strip with section-specific KPI sets.
- [ ] Combine the global and category AI hierarchy into one clear insight model.
- [ ] Turn the period switch into a real data range filter.
- [ ] Add local goals/settings for calories, protein, steps, weight, and sleep.
- [x] Implement sleep debt, sleep regularity, and bedtime/wake consistency.
- [ ] Add a recovery score using sleep, stress, heart rate, and activity with
  explicit uncertainty.
- [ ] Add data-quality badges for inferred, incomplete, stale, and cached values.
- [ ] Default charts to 7/14/30 days with optional full history.
- [ ] Move chat to a non-obstructive dock on mobile.

Acceptance: the user can understand the current state and next action in under
ten seconds without reading an AI essay.

### P2: Architecture and testability

- [ ] Introduce a canonical versioned data model.
- [ ] Extract domain calculations from dashboard rendering. Sleep aggregation
  and sleep metrics are the first extracted domain module.
- [ ] Extract source adapters from normalization and storage.
- [ ] Extract templates, CSS, and JavaScript into testable assets.
- [ ] Add prompt versions and output schema versions to AI caches.
- [ ] Add API integration, sanitizer, concurrency, schema, and browser tests.
- [ ] Add accessibility checks and chart table alternatives.
- [ ] Vendor frontend assets for reliable offline operation.

Acceptance: a source, metric, or screen can be added without editing a
multi-thousand-line file or changing unrelated behavior.

### P3: Product polish and OSS readiness

- [ ] Correlation explorer with minimum sample size and confidence labels.
- [ ] Calendar of anomalies, notes, workouts, symptoms, and missing data.
- [ ] Exportable weekly report with provenance and medical disclaimer.
- [ ] Backup/restore screen and encrypted portable archive option.
- [ ] Release versioning and tagged releases.
- [x] Add changelog, license, contributing guide, and security policy.
- [x] Synthetic read-only demo dataset that contains no personal information.
- [ ] Screenshots and an architecture diagram suitable for an OSS application.

Acceptance: a new user can run the demo safely, understand the architecture,
and contribute without receiving any private health data.

## Target Module Layout

```text
health_dashboard/
  domain/
    sleep.py
    nutrition.py
    body.py
    activity.py
    health.py
  sources/
    mobvoi.py
    health_connect.py
    google_fit.py
    yazio.py
    ksfit.py
  storage/
    atomic.py
    schemas.py
    repository.py
  ai/
    payloads.py
    prompts.py
    sanitizer.py
    cache.py
  web/
    server.py
    routes.py
    jobs.py
  ui/
    templates/
    static/
```

The current scripts should remain as thin compatibility wrappers until every
stage is tested:

```text
health_sync.py -> sources + domain + storage
gemini_analyzer.py -> ai
dashboard_server.py -> web
build_dashboard.py -> ui renderer
```

## Recommended Delivery Sequence

1. Finish P0 persistence, locks, source status, and log rotation.
2. Build the Overview screen and contextual KPI model.
3. Add goals/settings, recovery scoring, and data-quality badges.
4. Extract storage and domain calculations from the monolith.
5. Extract source adapters and add schema validation.
6. Extract UI assets and add browser/accessibility tests.
7. Create a synthetic demo mode and OSS documentation.

## Definition of "Ideal"

The dashboard is ideal when it is:

- private by default and safe against browser-origin attacks;
- honest about missing, stale, inferred, and cached data;
- useful before the user opens any detailed chart;
- resilient to a dead phone, failed source, interrupted write, or unavailable AI;
- readable on phone and desktop without covered content;
- medically cautious and explicit about uncertainty;
- reproducible, testable, versioned, and publishable with synthetic data only.
