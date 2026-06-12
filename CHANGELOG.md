# Changelog

All notable changes to this project will be documented in this file.

## Unreleased

## 0.1.0 - 2026-06-12

### Added

- deterministic read-only public demo mode;
- cross-domain Overview organized as today, trend, and prioritized action;
- sleep debt and regularity metrics;
- contextual KPI cards for every dashboard section;
- source freshness and runtime job status;
- schema validation and legacy migration for manual body measurements;
- contributor, support, security, and conduct documentation;
- issue and pull-request templates plus maintainer ownership metadata;
- GitHub Actions for tests, dependency auditing, CodeQL, and synthetic GitHub
  Pages deployment.

### Fixed

- future-dated synthetic AI timestamps no longer render as negative relative
  time;
- scheduled Windows sync no longer flashes child-process console windows.

### Security

- same-origin mutation protection;
- sanitized model-generated HTML;
- atomic persistence and single-flight background jobs;
- private health data, credentials, caches, and device-specific scripts excluded
  from version control;
- repeatable public-release audit rejects tracked private sidecars and
  high-confidence secret formats.
