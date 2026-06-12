# Health Dashboard V2

## Direction

Balanced dark health analytics interface: modern and expressive, but calmer
than the concept images. The UI should feel trustworthy enough for health data
and pleasant enough for daily personal use.

## Principles

- Preserve the working Python data pipeline and generated standalone HTML.
- Use one restrained dark material system instead of many unrelated card styles.
- Give every section one accent color; reserve red, amber, and green for meaning.
- Prefer readable density over decorative glow.
- Use SVG line icons instead of emoji as interface chrome.
- Keep important controls reachable with keyboard and 44px touch targets.
- Respect `prefers-reduced-motion`.

## Shell

- Sticky left navigation at desktop widths.
- Horizontal scrollable navigation below 900px.
- Compact header with identity, sync status, period control, and sync action.
- AI insight panel below the header.
- Global pulse strip for the most useful cross-category metrics.

## Section Accents

- Sleep: violet-blue.
- Body: cyan.
- Nutrition: warm orange.
- Food profile: emerald.
- Activity: electric blue.
- Health: rose.
- Notes: amber.

## Section Composition

### Sleep

Sleep score, stage history, latest-night composition, sleeping heart rate, and
space for future sleep debt, regularity, bedtime, and wake-time metrics.

### Body

Current body status, weight trend, body-composition trend, measurements,
computed ratios, and monthly focus.

### Nutrition

Daily energy status, macro and hydration progress, meal diary, calorie history,
macro history, and useful food ideas.

### Food Profile

Profile completeness, goals, preference chips, exclusions, free-form context,
and a clear preview of how recommendations change.

### Activity

Daily movement KPIs, combined steps and active-time chart, recent workouts,
training load, routes, and recovery-aware recommendations.

### Health

Heart rate, stress, SpO2, recovery context, important events, and long-term
trends. Medical-looking colors must not imply diagnosis.

### Notes

Fast capture, chronological history, tags, AI-discovered relationships, and
future reminders. Notes remain editable through the local server.

## Future Features

Features such as sleep debt should be added after the visual foundation is
stable. Their cards and chart slots can be introduced without changing the
overall shell.
