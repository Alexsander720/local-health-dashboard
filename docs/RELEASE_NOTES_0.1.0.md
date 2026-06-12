# Local Health Dashboard 0.1.0

The first public release turns a working personal health-data workflow into a
privacy-first OSS foundation.

## Highlights

- Cross-domain Overview organized as state, trend, and prioritized action.
- Sleep duration, stages, regularity, and estimated sleep debt.
- Body composition and manual measurement history.
- Nutrition diary, macro targets, food profile, activity, health, and notes.
- Deterministic read-only demo built entirely from synthetic data.
- Same-origin API protections, sanitized model HTML, atomic persistence, and
  single-flight background jobs.
- Responsive desktop and mobile layouts.

## Public Demo

Run locally:

```powershell
python -m pip install -r requirements.txt
python dashboard_server.py --demo --port 8788
```

The GitHub Pages workflow publishes the same synthetic dashboard as a static
read-only site.

## Important Limits

- This is an early OSS release and not a medical device.
- Personal mode currently expects locally exported and normalized data.
- Source adapters are still being separated from compatibility scripts.
- AI summaries are optional and must use cautious, non-diagnostic language.

