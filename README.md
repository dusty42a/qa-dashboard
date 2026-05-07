# qa-dashboard

Live Redmine target-version QA dashboard with AI risk assessment of commits.

## What it does

- Lists open target versions from Redmine.
- For a chosen version (URL: `/v/7.7`), shows the issues, their changesets, and a per-commit risk score from Claude Haiku.
- Refreshes upstream Redmine data at most once every 10 minutes per version, regardless of how many viewers have the page open.

## Run it

Copy `.env.example` to `.env`, fill in `REDMINE_KEY` and `ANTHROPIC_API_KEY`, then:

```bash
docker compose up --build
```

Open http://localhost:8080.

## Move it to an always-on box

```bash
git clone <this-repo> /opt/qa-dashboard
cd /opt/qa-dashboard
cp .env.example .env && $EDITOR .env
docker compose up -d
```

The SQLite cache lives in `./data/qa.sqlite`. Nothing else is stateful.

## Tests

The harness in `tests/` runs fully offline — it stubs out `app.redmine.*` and
`app.risk.score_commit` so SQLite, FastAPI, and the refresh/cache logic all
exercise real code paths against canned data. No `REDMINE_KEY` or
`ANTHROPIC_API_KEY` needed.

```bash
pip install -e ".[dev]"
pytest
```

If you've only got Docker, run them inside a one-shot container:

```bash
docker compose run --rm --entrypoint "" qa-dashboard sh -c "pip install '.[dev]' && pytest"
```

## Configuration

See `.env.example`. Notable non-obvious options:

- `REDMINE_VERIFY_SSL=false` — required for `dev.workbooks.com` because its issuer isn't in the standard trust store. Set to `true` for any Redmine that has a real cert.
- `UPSTREAM_TTL_SECONDS=600` — minimum gap between Redmine fetches for the same version. Frontend polling is independent and faster (60s); it just hits the cache between upstream fetches.
- `DIFF_LINE_CAP=2000` — diffs longer than this are truncated before going to Haiku. The model is told the diff was truncated and treats it as higher uncertainty.
- `RISK_ENABLED=false` — turn off Claude calls entirely (useful for offline dev or if you've blown your Anthropic budget).
