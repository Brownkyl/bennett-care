# bennett-care

Local, private toolkit for analyzing seizure data for Bennett and generating pre-visit summaries for clinic appointments.

**All patient data stays on this machine.** No external APIs, no telemetry. See `CLAUDE.md` for full constraints and conventions.

## Setup

Requires [`uv`](https://docs.astral.sh/uv/) and Python 3.11+ (uv will install Python if needed).

```bash
uv sync --extra dev
```

## Usage

Place the seizure log at `data/seizure_log.xlsx` (gitignored), then:

```bash
uv run bennett-care visit-prep \
  --log data/seizure_log.xlsx \
  --visit-date 2026-08-04 \
  --lookback 90 \
  --output output/
```

## Development

```bash
uv run pytest
```

## Layout

```
src/bennett_care/   # package source
tests/              # pytest tests (use fixtures, never the real log)
data/               # local Excel logs — gitignored
output/             # generated Word docs — gitignored
reference/          # clinical PDFs/notes — gitignored
```

See `CLAUDE.md` for the full data model, design decisions, and engineering conventions.
