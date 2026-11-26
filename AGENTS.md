# Agent notes for octograph

A Python CLI that loads Octopus Energy data (consumption, pricing, intelligent dispatches) into InfluxDB. It is configured with an INI file. See `README.md` for user-facing behaviour.

## Workflow rules

- **Do not push, open PRs or merge without the maintainer's review.** Prepare changes locally and ask first.
- Never commit private data: real account numbers, MPANs/MPRNs, meter serial numbers, addresses, locations, internal hostnames or API keys. Tests and docs use fictional values (e.g. `A-1234ABCD`, MPAN `1900000000001`, "Old Home"/"New Home").
- `octograph.*.ini` files are local configs and are gitignored (except `octograph.example.ini`). They may contain secrets, so don't read them into commits, PRs or logs.
- Stage files explicitly and check `git status` before committing; don't commit `__pycache__`, `venv/` or `scratch/`.

## Remotes and releases

- PRs go to `Yanson/octograph` `main` (remote `Yanson`). `DragonQ` and `stevenewey` are other forks; their CI lacks Docker Hub credentials.
- There are no git tags or semver. `.github/workflows/build.yaml` publishes `yansonb/octograph:<run_number>.<run_attempt>`, `sha-<short sha>`, the branch name, and `latest` (on `main` only). The release "version" is the run number of the `main` build after the merge.

## Code layout

- `app/octopus_to_influxdb.py`: CLI entry point and collection logic (`OctopusToInflux`). Imports sibling modules without the `app.` prefix, as the Docker image runs it as a script.
- `app/date_utils.py`: local-day alignment and timezone helpers.
- `app/octopus_graphql_api_client.py`, `app/octopus_api_client.py`: Octopus API clients.
- `app/series_maker.py`, `app/eco_unit_normaliser.py`, `app/intelligent_dispatches.py`: pricing and dispatch helpers.

## Behaviour to preserve

- Backwards compatibility matters: existing configs must produce identical points and tags. Don't rename or change the meaning of CLI options or config keys.
- Tags resolve as global `additional_tags`, then `[additional_tags:<MPAN|MPRN>]`, then `[additional_tags:<meter serial>]`. Meters inherit their meter point's overrides.
- `--from-date=latest` looks up the last consumption point over 30 days, filtered on the resolved tags.
- Each property is collected only within its active window `[moved_in_at, moved_out_at)`, aligned to whole intervals from the local day start. A meter point with no recent data fails on its own; the run continues and exits non-zero at the end.

## Testing

```sh
venv/bin/pytest -q -p no:warnings tests
```

- `tests/octograph_harness.py` runs the CLI end-to-end against fake InfluxDB and Octopus clients, with a frozen clock (`NOW`). Import it before `app` modules, because it adds `app/` to `sys.path`.
- `tests/fixtures/regression_existing_config.json` is a golden file of points from an existing config. Regenerate it only from the **previous release's** code (stash `app/` changes first) with `OCTOGRAPH_UPDATE_GOLDEN=1 venv/bin/pytest tests/test_regression_existing_config.py`. Regenerating from new code defeats the regression check.
- Tests depend on timezone handling; the harness uses `Europe/London`.
- Known issue: the Dockerfile `test` stage runs `tests/unit`, which doesn't exist.
