# Contributing

Thank you for helping improve Local Health Dashboard.

## Start With The Demo

```powershell
python -m pip install -r requirements.txt
python dashboard_server.py --demo --port 8788
```

Never use another person's health export for development or tests. Add or
extend synthetic fixtures in `health_dashboard/demo_data.py`.

## Development Workflow

1. Create a focused branch.
2. Add a failing test for behavior changes.
3. Make the smallest implementation that passes.
4. Run the complete suite.
5. Check desktop and mobile layouts for UI changes.

```powershell
python -m unittest discover -s tests -q
```

## Pull Requests

Keep changes scoped and explain:

- the user problem;
- the data or privacy implications;
- how the change was tested;
- whether any metric is inferred or medically sensitive.

Do not include generated dashboards, screenshots containing personal data,
database exports, tokens, cookies, local paths, or AI cache contents.

## Health Claims

Use cautious language. Distinguish observations, estimates, correlations, and
diagnoses. The project must not present an inferred metric as a medical fact.
