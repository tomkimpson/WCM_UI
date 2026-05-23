# Handoff — 2026-05-23 19:50

## Goal
Drive the worker image end-to-end from a JSON parameter file: validate, merge with defaults, invoke wcEcoli with resolved CLI flags. Stage 1b of the 7-stage build order in `docs/plans/2026-05-22-wcm-frontend-design.md`.

## Status
**Stage 1b complete.** All 8 plan tasks committed; full host-side suite (23 unit tests in `test_schema`/`test_merge`/`test_validate`/`test_run_flag_resolution`) plus the smoke (~13 min) and parametric (~15 min) e2e tests pass locally. CI green on `ubuntu-latest` in 38m40s (run 26326541567 on `main` at `446d810`). Repo is public at https://github.com/tomkimpson/WCM_UI.

## What changed this session
See `log.md`.

## Open questions
- **Next direction.** Stage 1c (YAML override layer + variant-based knobs — gene knockouts, media composition; requires traversing `vendor/wcEcoli/models/ecoli/sim/variants/`) versus jumping straight to Stage 2 (AWS Batch + S3 wiring). The worker is now parametric-by-env-var, which is the right shape for Batch — so Stage 2 is unblocked. Stage 1c grows the curated knob set toward the design doc's 15–25 target before going cloud-native; Stage 2 ships infrastructure before broadening the surface. No strong technical reason either way.
- **`/ultrareview` of the Stage 1b branch?** Could pressure-test the 14 commits before moving on. User-initiated only.

## Blockers / problems
None.

## Next steps
1. Commit untracked session files (`CLAUDE.md`, `handoff.md`, `log.md`) so they travel with the repo. None are gitignored.
2. Pick Stage 1c or Stage 2 (see Open questions). Either way: draft a plan in `docs/plans/2026-05-23-stage{1c,2}-*.md` before executing — Stage 1b's 8-task TDD plan worked well as a model.
3. Address the Node 20 GitHub Actions deprecation before 2026-06-02 — `actions/checkout@v4`, `actions/setup-python@v5`, `docker/setup-buildx-action@v3` need to be bumped to Node-24-compatible majors. Currently a non-blocking annotation on every CI run.

## Non-obvious context
- **Bare `pytest tests/...` requires the `pyproject.toml` rootdir fix** (`pythonpath = ["."]`). Without it, any test importing `worker.*` raises `ModuleNotFoundError` because pytest doesn't extend `sys.path` from the project root when the only `conftest.py` lives under `tests/`. Don't delete `pyproject.toml`.
- **Keystone proof-point for Stage 1b** is `attrs["lengthSec"] == 30.0` in the Main listener's `attributes.json` after `docker run -e PARAMS_JSON='{"simulation":{"length_sec":30}}'`. wcEcoli writes the exact CLI-passed value into this file — strict equality is the right assertion shape, far stronger than the plan's first-guess "is there a `time` key?".
- **`jsonschema` sort key must coerce to `tuple(str(p) for p in e.absolute_path)`**. The raw `absolute_path` is a `deque` mixing `str` keys and `int` array indices; sorting raw `deque`s raises `TypeError` once arrays enter the schema (Stage 1c's variant knobs will). Already fixed proactively in `worker/validate.py`.
- **`worker.run` catches `OSError`, not just `FileNotFoundError`.** Covers the realistic operator mistakes: `PARAMS_JSON` pointing at a directory, a permission-denied file, or a broken symlink. All exit 64 (EX_USAGE) with a `param error: …` prefix.
- **Image layer ordering keeps iteration cheap.** `COPY worker/ /wcEcoli/worker/` is the last expensive layer — worker-only edits invalidate only the trailing ~74 kB layer, not the ~3-min wcEcoli requirements install above it. Don't reorder.
- **The 5 Stage 1b knobs are deliberately scalar-only** (`length_sec`, `seed`, `generations`, `init_sims`, `parca_cpus`). Each maps 1:1 onto a `runSim.py` / `runParca.py` CLI flag and was chosen because (a) the effect is observable in a short sim and (b) no wcEcoli internals are touched. Variant-based knobs (gene KOs, media) require deeper work in `vendor/wcEcoli/models/ecoli/sim/variants/` and are explicitly deferred to Stage 1c.
- **Direct push to `main` is the working model** for this repo (no PR-review gate; single developer). Authorization stands for explicit, scoped pushes — keep asking for novel pushes if context shifts.
