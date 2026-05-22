# WCM_UI

Web frontend for running the [wcEcoli](https://github.com/CovertLab/wcEcoli) whole-cell model on cloud compute.

See [docs/plans/2026-05-22-wcm-frontend-design.md](docs/plans/2026-05-22-wcm-frontend-design.md) for the design.

## Repository layout

- `worker/` — Dockerised wcEcoli runner.
- `tests/` — Host-side integration tests (pytest).
- `docs/` — Design docs and plans.

## Quick start

```bash
make smoke    # build the image and run a smoke simulation
```
