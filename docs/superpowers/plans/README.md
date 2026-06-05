# Implementation Plans Index

Task-by-task, TDD implementation plans for the [Seekr roadmap](../../ROADMAP.md).
Each plan is self-contained and independently shippable: header block, exact `file:line`
references, failing-test-first steps with complete code, PowerShell + `pytest` commands,
conventional-commit messages, a Definition of Done, and notes for the executing agent.

**How to execute a plan:** open it and follow the `superpowers:subagent-driven-development`
(fresh subagent per task, review between tasks) or `superpowers:executing-plans` workflow.
Steps use `- [ ]` checkboxes for tracking.

> All P2–P4 plans state their **design assumptions** at the top — they encode decisions
> (vector store, connector auth, versioning strategy, etc.) that you may want to confirm
> before execution. The three **Open questions** at the bottom of the roadmap affect several.

---

## Pre-existing plans (already landed / in progress)

| Plan | Status |
|---|---|
| [2026-05-17-acl-foundation](2026-05-17-acl-foundation.md) | Landed — ACL tables + `visible_document_ids_subquery`, enforced on search only |
| [2026-05-17-job-queue](2026-05-17-job-queue.md) | Landed — `JobStore` + `Worker`; `index_paths` / `ai_*` migrated |
| [2026-05-15-light-ui-redesign](2026-05-15-light-ui-redesign.md) | Prior UI work |
| [2026-05-15-search-fixes-autocomplete](2026-05-15-search-fixes-autocomplete.md) | Prior search work |

---

## P0 — Stabilise & secure

| Plan | Covers |
|---|---|
| [security-hardening](2026-06-05-security-hardening.md) | Error sanitisation, non-root container, upload magic-byte/containment, pinned deps, SMB credential file, subprocess timeouts |
| [structured-logging](2026-06-05-structured-logging.md) | `logging_config.py`, replace 7 silent `except: pass`, bounded session/rate-limit eviction |
| [ci-and-quality-gates](2026-06-05-ci-and-quality-gates.md) | GitHub Actions CI, ruff + mypy config, tests for extractors/upload/update |

## P1 — Finish what's started

| Plan | Covers |
|---|---|
| [acl-enforcement-completion](2026-06-05-acl-enforcement-completion.md) | Set `owner_principal_id` on ingest; filter every remaining document endpoint |
| [acl-management-api-ui](2026-06-05-acl-management-api-ui.md) | Group + grant CRUD, `can_write`, `/api/acl/*` + `/api/groups/*`, Config → Access tab |
| [job-queue-completion](2026-06-05-job-queue-completion.md) | Migrate `ai_pull` + update-status, `cancelled` state, `GET /api/jobs`, global Jobs dashboard |
| [search-pagination-and-fixes](2026-06-05-search-pagination-and-fixes.md) | Offset pagination + "Load more", FTS parse-error handling, opt-in scheduled re-index |

## P2 — Product depth

| Plan | Covers |
|---|---|
| [hybrid-semantic-search](2026-06-05-hybrid-semantic-search.md) | Ollama embeddings, `block_embeddings` / sqlite-vec, RRF hybrid ranking, recency/field boosts |
| [saved-searches-and-history](2026-06-05-saved-searches-and-history.md) | Server-side history + named saved searches restoring query + filters |
| [new-extractors-and-ocr](2026-06-05-new-extractors-and-ocr.md) | `.xlsx`/`.csv`/`.html`/`.eml` extractors + per-source OCR language, force-OCR, confidence |
| [external-connectors](2026-06-05-external-connectors.md) | WebDAV/Nextcloud + S3 connectors, `connector_index` job, owner-aware ingest |
| [duplicate-detection](2026-06-05-duplicate-detection.md) | sha256 exact + normalized content-hash near-dup grouping, keep-one/remove UI |
| [ai-rag-and-tagging](2026-06-05-ai-rag-and-tagging.md) | RAG summarisation with citations, bulk auto-tagging, AI output validation + `ai_decisions` provenance |

## P3 — Scale, observability & UI polish

| Plan | Covers |
|---|---|
| [observability-health-metrics](2026-06-05-observability-health-metrics.md) | `/health`, `/ready`, Prometheus `/metrics`, queue/job/throughput metrics, Docker healthcheck |
| [backup-restore-rollback](2026-06-05-backup-restore-rollback.md) | WAL-safe `Connection.backup()`, restore, export/import, scheduled backups, update auto-rollback |
| [multi-instance-readiness](2026-06-05-multi-instance-readiness.md) | `SessionStore` + `RateLimiter` (SQLite default / optional Redis), configurable CORS |
| [ui-accessibility-and-theming](2026-06-05-ui-accessibility-and-theming.md) | WCAG 2.1 AA pass, dark mode, inline-style cleanup, shared auth-gate partial, skeletons |
| [frontend-modularisation](2026-06-05-frontend-modularisation.md) | Split `app.js` into native ES modules, one feature group per commit |

## P4 — Exploratory / nice-to-have

| Plan | Covers |
|---|---|
| [document-preview](2026-06-05-document-preview.md) | Inline ACL-gated preview, vendored PDF.js, text/markdown/image rendering |
| [audit-log](2026-06-05-audit-log.md) | `audit_log` table, instrumentation at sensitive routes, admin `GET /api/audit` + tab |
| [user-preferences](2026-06-05-user-preferences.md) | Server-side per-user theme / results-per-page / default filters |
| [webhooks-notifications](2026-06-05-webhooks-notifications.md) | Signed outbound webhooks, queue-backed retry, `index.completed` event |
| [plugin-extractor-api](2026-06-05-plugin-extractor-api.md) | Entry-point + drop-in extractor discovery, error isolation, example plugin |
| [i18n](2026-06-05-i18n.md) | JSON catalogs (en/de), `t()` helper, language selector, catalog-integrity tests |
| [api-versioning](2026-06-05-api-versioning.md) | `/api/v1/*` via scope-rewrite middleware (back-compat), OpenAPI polish |

---

## Suggested execution order

1. **P0** — `structured-logging` + `security-hardening` + `ci-and-quality-gates` first; they make everything after safer to change.
2. **P1** — `acl-enforcement-completion` → `acl-management-api-ui`, then `job-queue-completion` + `search-pagination-and-fixes`.
3. **P2** — `hybrid-semantic-search` (headline feature) + 1–2 extractors.
4. **P3 / P4** — fold in alongside feature work as appetite and the open questions allow.

### Cross-plan dependencies to watch
- `ui-accessibility-and-theming` (dark-mode toggle) ↔ `user-preferences` / `i18n` (persisted theme + language).
- `job-queue-completion` adds the `cancelled` state used opportunistically by long-running jobs in other plans.
- `observability` `/ready` is consumed by `backup-restore-rollback`'s update auto-rollback.
- `hybrid-semantic-search` embeddings can later sharpen `duplicate-detection` (kept independent for now).
- `webhooks-notifications` "new match" event builds on `saved-searches-and-history`.
