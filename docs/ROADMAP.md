# Seekr — Development Roadmap

> Living document. Created 2026-06-05. Covers fixes, hardening, feature work and UI/UX
> across the whole stack (Crawler → Extractors → Services → Index → Web).
>
> Scope of the codebase today: FastAPI backend (`app.py` ~1,650 lines), SQLite + FTS5
> index, vanilla-JS frontend (`app.js` ~1,780 lines, 5 Jinja templates), optional Ollama
> AI, Docker + nginx TLS deployment. ACL foundation and persistent job queue are landed;
> several follow-ups remain.

> **Executable plans:** every item below has a task-by-task TDD implementation plan in
> [`docs/superpowers/plans/`](superpowers/plans/README.md) (see that index for the full
> phase → plan mapping).

## How to read this

Items are grouped into **phases by priority**, not dates:

- **P0 — Stabilise & secure** — correctness, security and data-safety gaps that should be closed before adding surface area.
- **P1 — Finish what's started** — complete the ACL and job-queue work already begun; close obvious UX rough edges.
- **P2 — Product depth** — features that materially expand what Seekr can do (semantic search, connectors, richer ingest).
- **P3 — Scale & polish** — observability, multi-instance readiness, UI redesign, accessibility.
- **P4 — Nice-to-have / exploratory** — opportunistic improvements.

Each item carries an effort hint: **S** (≤1 day), **M** (a few days), **L** (1–2 weeks+).

---

## P0 — Stabilise & secure

### Security
- **[S] Sanitise error responses.** Several endpoints do `raise HTTPException(detail=str(e))` (e.g. `app.py` config/SSL/mount/upload paths), leaking internal exception text. Return generic messages; log the detail server-side.
- **[M] Run the container as non-root.** `Dockerfile` has no `USER`; the app (and the in-app update subprocess) run as root. Add a dedicated user, adjust volume ownership, keep only the capabilities the mount feature needs.
- **[S] Harden upload path validation.** Upload target containment uses `resolve()` but is symlink-exploitable; apply the same explicit containment + `..` rejection used by the reorganize path, and add magic-byte (`libmagic`) sniffing so the extension allowlist can't be bypassed.
- **[S] Pin security-sensitive dependencies.** `cryptography`, `PyYAML`, `pillow`, `jinja2`, `python-multipart` are unpinned in `requirements.txt`. Pin to known-good ranges and add a lockfile (`pip-tools`/`uv`).
- **[M] Protect SMB/NFS credentials.** Mount credentials are passed as subprocess args (visible in `/proc/<pid>/cmdline`). Use a credentials file (`-o credentials=`) and validate the remote path. Also validate `req.path` on mount/unmount/test endpoints.
- **[S] Encrypt or externalise secrets at rest.** HA API keys and Ollama config live in `config.json` in plaintext. At minimum document the exposure; ideally move secrets to env/secret store.

### Robustness & correctness
- **[M] Introduce structured logging.** `app.py` has effectively zero logging; ~8 `except Exception: pass` blocks silently swallow config-load and subprocess failures. Add a `logging` config, and replace silent passes with `log.warning/exception`. This is the single highest-leverage maintainability fix.
- **[S] Make session + rate-limit state robust.** Both live in unbounded in-memory dicts, lost on restart and not thread-safe. Add periodic eviction now; plan a persistent backing store (see P3 multi-instance).
- **[S] Add timeouts to all subprocess calls.** Some `git`/shell invocations omit `timeout=`; a hung child blocks a worker thread.

### Tests / CI
- **[M] Add a CI pipeline.** No `.github/workflows` exists today. Add GitHub Actions: install deps, run `ruff`, run `pytest -q`. Gate merges on green.
- **[S] Add `ruff` (and optionally `mypy`) config.** AGENTS.md already mandates Ruff/PEP8 and type hints — enforce it.
- **[M] Cover the untested high-risk paths:** file extraction (PDF/DOCX/PPTX with fixture files), upload validation/path-traversal, SSL generate/upload, and the update flow (mock subprocess).

---

## P1 — Finish what's started

### Complete ACL enforcement
The ACL foundation (`principals`, `user_groups`, `document_acl`, `acl_service.visible_document_ids_subquery`) is landed and enforced on **search/browse only**. Everything else still reads the index unfiltered.

- **[M] Filter every document-returning endpoint** through the ACL subquery: `/api/folders`, `/api/source-folders`, `/api/status` / `/api/ha/status` counts, `/api/files/open`, mark/tag endpoints, and the AI reorganize/suggest sampling queries.
- **[M] Set `owner_principal_id` on ingest.** Today it's never populated, so owner-based visibility never fires and everything falls back to the `public` group. Populate it on upload (uploader = owner) and on indexed crawl (configurable default owner/group).
- **[L] ACL management API + UI.** No `/api/acl/*` write surface exists. Add endpoints to grant/revoke read/write per document or group, plus a Config → Access tab. Wire `write` permission (currently only `read` is ever queried).
- **[S] Group management.** Expose creating groups and assigning users (`user_groups`) via the Users config tab.

### Complete the job queue
Persistent queue (`JobStore` + `Worker`) handles `index_paths`, `ai_suggest_structure`, `ai_reorganize`. Two job types remain in-memory.

- **[M] Migrate `ai_pull_model`** to a persistent `ai_pull` job kind with streamed progress (currently a fire-and-forget daemon thread in the `ai_jobs` dict).
- **[S] Decide `_update_job` policy.** The system-update job replaces the process, so persisting it is odd — but record a final status row before exec so the UI can confirm the outcome after restart.
- **[M] Global Jobs/Tasks view.** Add `GET /api/jobs` (owner/admin scoped, already supported by `JobStore.list_jobs`) and a frontend dashboard listing all jobs with state, progress, retry count, and a re-enqueue button for `interrupted` jobs.
- **[S] Job cancellation.** Add a `cancelled` state + `POST /api/jobs/{id}/cancel`; have handlers check a cooperative cancel flag at progress checkpoints.

### Search & ingest rough edges
- **[M] Result pagination.** Search is hard-capped at ~25 results with no "load more". Add limit/offset (or keyset) paging in `search_service.search` and the UI.
- **[S] Surface FTS parse errors gracefully.** Unmatched quotes throw an FTS5 error; catch and return a friendly "check your query syntax" message.
- **[S] Incremental / scheduled re-indexing.** Today indexing is manual. Add an optional periodic crawl (cron-style) using the existing queue.

---

## P2 — Product depth

### Search quality
- **[L] Semantic / hybrid search.** Search is pure FTS5 keyword today. Add local embeddings (via Ollama `embeddings` API or `sentence-transformers`) stored in a vector table (`sqlite-vec`/`sqlite-vss`), and blend vector similarity with BM25 for hybrid ranking. Biggest single capability upgrade.
- **[M] Ranking improvements.** Add recency boost, filename/title field weighting, and per-document score aggregation tuning on top of BM25.
- **[M] Saved searches & search history server-side.** Recent searches are localStorage-only; persist per user and allow saving named queries/filters.
- **[S] Query assist.** Did-you-mean / autocomplete from the FTS vocabulary and the tag set.

### Ingestion breadth
- **[M] More file types:** `.xlsx`/`.csv` (tabular extraction), `.html`, `.eml`/`.msg` (email), `.epub`, images with OCR-only content. Each is a new extractor following the existing `extractors/base.py` contract.
- **[M] Better OCR pipeline.** Make OCR language configurable per source, add a "force OCR" toggle for scanned PDFs, and surface OCR confidence.
- **[L] External connectors.** The ACL plan explicitly anticipates a Nextcloud/WebDAV connector (owner-aware ingestion). Add connectors for Nextcloud, WebDAV, and S3-compatible storage, each populating `owner_principal_id`.
- **[M] Duplicate detection & dedup.** SHA-256 fingerprints already exist; surface near-duplicate/identical documents and let the user merge or hide them.

### AI features
- **[M] AI-assisted search summarisation.** "Summarise the top results" / RAG-style answer over retrieved documents, with citations back to source blocks.
- **[M] Bulk auto-tagging.** Extend the existing single-file AI tag suggestion to a batch job over the whole index.
- **[S] Make AI outputs schema-validated and auditable.** AGENTS.md requires validating LLM output before persisting and keeping provenance — add JSON-schema validation and store the model/prompt used for each AI decision.

---

## P3 — Scale, observability & UI polish

### Observability & operations
- **[S] Health endpoints.** Add `/health` (liveness) and `/ready` (DB reachable, worker alive) for container orchestration and the nginx upstream.
- **[M] Metrics.** Expose Prometheus metrics (request latency, index throughput, queue depth, job success/fail counts).
- **[M] Backup/restore as a first-class feature.** Backups only happen as a side-effect of `update.sh`. Add scheduled backups, a restore command, and a documented disaster-recovery procedure. Add data export/import (documents + tags + ACLs).
- **[S] Update rollback.** `update.sh` has no rollback if build/startup fails; capture the previous image/commit and auto-revert on failed health check.

### Multi-instance readiness
- **[L] Externalise session + rate-limit state** (Redis or a DB table) so more than one app replica can run behind the proxy. The job queue's atomic `UPDATE ... RETURNING` claim already anticipates multiple workers.
- **[M] Configurable CORS.** Starlette defaults are permissive; add explicit `CORSMiddleware` with an allowlist.

### UI / UX overhaul
The frontend is clean vanilla JS with a token-based CSS design system, but has clear gaps.

- **[M] Accessibility pass.** No ARIA anywhere today. Add `aria-live` for toasts/progress, `aria-busy` on async regions, labelled controls, visible focus rings, Escape-to-close, and `role="status"` for feedback. Target WCAG 2.1 AA.
- **[S] Dark mode.** Design system already uses CSS variables in `:root`; add a `[data-theme="dark"]` token set and a toggle (respect `prefers-color-scheme`).
- **[M] Global Jobs dashboard** (pairs with the P1 backend `GET /api/jobs`) — one place to watch all indexing/AI jobs instead of per-action progress bars.
- **[S] Move inline styles to CSS classes.** ~60 inline `style=` usages across templates; consolidate into `styles.css` for consistency and maintainability.
- **[S] Loading skeletons & better empty/error states.** Replace bare progress bars with skeleton loaders; make empty/error states consistent across pages.
- **[M] Responsive/mobile polish.** Single 640px breakpoint today; add intermediate breakpoints, a proper mobile nav, swipe-friendly result cards, and a mobile-aware upload flow.
- **[S] De-duplicate the auth-gate markup** repeated in every template into a shared partial.
- **[M] Consider a light build step / modularisation.** `app.js` is a single 1,780-line file. Optional: split into ES modules (still no framework) for maintainability, or adopt a minimal bundler.

---

## P4 — Exploratory / nice-to-have

- **[M] Document preview in-browser** (PDF.js viewer, rendered DOCX/PPTX previews) instead of raw file download.
- **[M] Full audit log** of who searched/opened/moved what — leverages the provenance goals in AGENTS.md.
- **[S] Per-user UI preferences** (default filters, results-per-page, theme) persisted server-side.
- **[M] Webhooks / notifications** on index completion or new matching documents for a saved search.
- **[L] Plugin/extractor API** so third parties can register new file-type extractors without forking.
- **[S] i18n.** The product/docs mix German and English; add a translation layer for the UI.
- **[M] REST API versioning + OpenAPI polish** (the Swagger/wiki page already embeds the spec) and a documented public API contract for the HA-style integrations.

---

## Suggested sequencing

1. **First slice (P0):** structured logging + error sanitisation + CI/ruff + non-root container. Low risk, high leverage, makes everything after it safer to change.
2. **Second slice (P1):** finish ACL enforcement + `owner_principal_id` on ingest, then the Jobs dashboard (`GET /api/jobs` + UI) and pagination. These complete half-built features users can already see.
3. **Third slice (P2):** hybrid semantic search — the headline capability — plus 1–2 new extractors.
4. **Ongoing (P3):** accessibility + dark mode + observability folded in alongside feature work.

---

## Open questions to resolve before starting

- **Tenancy model:** is Seekr single-household/single-team (current `public`-group default is fine) or genuinely multi-tenant (drives how hard ACL enforcement and owner assignment must be)?
- **Semantic search appetite:** acceptable to require an embeddings model in Ollama, or must keyword-only remain a first-class mode for low-resource hosts?
- **Deployment target:** always single-node Docker, or is multi-replica/k8s on the horizon (drives the P3 session/state externalisation work)?
