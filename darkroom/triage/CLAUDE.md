# `darkroom triage` (transient cleanup tool)

A web UI for cleaning up the **existing** NAS archive — not part of the steady-state
ingest pipeline. `triage scan` walks the archive, flags problems (placeholder FITS
`OBJECT`, RA/DEC mismatches, mis-filed calibration, legacy session naming), and
proposes corrections; `triage serve` (port 8002) lets you review each item and
apply move/rename/copy-corrected/trash, or revert a prior action.

State lives in a **separate `triage.db`** (default `<archive>/triage.db`), distinct
from `astro_catalog.db` — triage does not write to the catalog, so the "catalog is
the single source of truth" rule still holds. This tool is expected to be removed
once the archive backlog is cleaned up; treat it as scaffolding, not core.
