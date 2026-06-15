# Implementation Plan — Deterministic AI Assistant Platform

> **Status:** DRAFT for review (not started)
> **Owner:** @abel
> **Last updated:** 2026-06-15
> **Companion to:** [design-spec.md](design-spec.md) — this plan turns that spec into small, reviewable, individually-validated build steps.

This document is the **build plan**: a `Plan → Phases → Tasks` breakdown (mirroring the spec's own vocabulary, §6B) that we execute **one small step at a time**. You review this plan first; then we implement task-by-task.

---

## How to use this plan

**The two rules you set, applied to every task:**
1. **One thing per step.** Each task changes *one* logical thing and is small enough to review in a single sitting. No drive-by edits, no bundling.
2. **Validate after every step.** Every task ends with a **runnable validation** (a test and/or a CLI smoke check) that must pass *before* we move on. If it can't be validated, it's split until it can.

### Definition of Done (every task)
A task is "done" only when **all** of these hold:
- [ ] It implements exactly the one stated goal — nothing extra.
- [ ] It adds or updates a **test** (or a documented runnable check) for the new behavior.
- [ ] Its **validation command passes** locally (see *Validation toolbox*).
- [ ] Existing tests still pass (no regressions).
- [ ] Code is formatted + lint-clean; no secrets, no unrelated diffs.
- [ ] We pause for your review at the **phase checkpoints** (✅ markers below).

### Validation toolbox (commands a task may use)
Run from `backend/` unless noted.
- **Unit tests:** `python -m pytest -q` (or a single file: `python -m pytest tests/test_x.py -q`)
- **Live integration tests (manual, opt-in):** `python -m pytest -m integration` — calls a **real** model; excluded from the default run, and **skipped** unless a provider token is configured (`.env` / env). Proves real model output survives our strict validation + a simple ask runs end-to-end.
- **Type/lint (added in P0.2):** `python -m ruff check .` and `python -m ruff format --check .`
- **Config check (no network):** `python -m app.cli.verify --dry-run`
- **Live auth smoke (manual, opt-in):** `python -m app.cli.verify --live`
- **End-to-end ask (from P4):** `python -m app.cli.ask "what is 2+2?"`
- **DB inspect (from P1):** `python -m app.cli.db --schema`

### Status legend
`[ ]` not started · `[~]` in progress · `[x]` done · `✅` = **review checkpoint** (stop, you review, then continue)

---

## Guiding constraints (carried from the spec)
- **AI is never the control path.** Deterministic code validates every model output and decides what runs (§3). Tasks that touch AI always include a validation/schema step.
- **Local-first, chat-first.** Build the deterministic core + a CLI harness first; Telegram and the web app come later.
- **Everything recoverable from folders + DB.** Storage tasks come early so later layers persist state.
- **Secrets only in env or the auth cache**, never in DB/logs/prompts (§12).

## Already in place (partial Phase 0 — do **not** rebuild)
- `backend/app/config/settings.py` — env settings + `config/models.yaml` loader.
- `backend/app/advisor/providers.py` — `AIProvider`, `OpenAICompatibleProvider`, `GitHubModelsProvider` (Route B / PAT), `build_provider`.
- `backend/app/advisor/redaction.py` — secret redaction guard.
- `backend/app/cli/verify.py` — provider/config smoke check.
- `config/models.yaml`, `.env.example`, `pyproject.toml` (pydantic, httpx, PyYAML, pytest).

> The existing **Route B (GitHub Models PAT)** provider means the whole core can be built and run **today** with a PAT — device-flow login (Route A) and chat pairing are deferred to P7, just before channels.

---

## Phase roadmap (recommended order)

| Phase | Theme | First end-to-end milestone | Spec |
|-------|-------|----------------------------|------|
| **P0** | Foundations & test harness | `pytest` + lint + `verify --dry-run` green (no network) | §7, §12 |
| **P1** | Storage & data model | DB schema creates + round-trips | §9 |
| **P2** | Skills as APIs | a read-only skill runs through the runtime | §8 |
| **P3** | AI advisor wrapper | model output validated into a schema (fake provider) | §7 |
| **P4** | Roles, envelope & control loop | **a simple ask answered end-to-end via CLI** | §6, §6A, §6D |
| **P5** | Memory & library | search + TTL + final report on disk | §9, §9.1, §9.2 |
| **P6** | Complex jobs (task **and** feature) | a task job runs phases with sign-off; a feature job emits gated generated code | §6B, §5 |
| **P7** | Auth (device flow) & owner pairing | `login` + `pair` + allowlist | §7.2, §10.1 |
| **P8** | Channels (Telegram) | answer a real Telegram message | §10 |
| **P9** | First-run setup & onboarding | one `setup` command configures + verifies an existing checkout | §7.2, §10, §13 |
| **P10** | Web app (FastAPI + dashboard) | Requests/System/Reports pages | §11 |

Each phase ends with a **✅ review checkpoint**. Phases are mostly sequential; P7 can move earlier if you want pairing before the CLI harness.

---

## P0 — Foundations & test harness

> Goal: a clean, validated baseline so every later step has a test to run.

- [x] **T0.1 — Add the test harness.** Create `backend/tests/` with `conftest.py` and one trivial `test_smoke.py`. Wire `pytest` config in `pyproject.toml`.
  - *Validate:* `python -m pytest -q` → 1 passed.
- [x] **T0.2 — Add ruff (lint + format) config.** Add `ruff` to dev deps + config; fix any existing findings.
  - *Validate:* `python -m ruff check .` and `python -m ruff format --check .` → clean.
- [x] **T0.3 — Characterize redaction with tests.** Add `tests/test_redaction.py` covering each rule (PAT, openai key, bearer, secret-assignment, connection string, private key) + a "no false-positive on normal prose" case.
  - *Validate:* `python -m pytest tests/test_redaction.py -q`.
- [x] **T0.4 — Characterize config loading with tests.** Add `tests/test_settings.py` for `load_models_config` + `provider_for_role` (valid + unknown-role + unknown-provider).
  - *Validate:* `python -m pytest tests/test_settings.py -q`.
- [x] **T0.5 — Add `config/policies.yaml` + typed loader.** Single source for knobs (`max_phase_declines: 3`, `max_improvement_iterations: 2`, `max_append_reroutes: 1`, `max_concurrent_jobs: 3`, `junior_session_idle_minutes: 15`, `progress_updates: phase`). Loader in `app/config/policies.py`.
  - *Validate:* `tests/test_policies.py` asserts defaults load + override works.
- [x] **T0.6 — Provider unit test with a fake transport.** Test `OpenAICompatibleProvider.complete` against a mocked `httpx` (no network) to lock the request/response shape + that redaction is applied.
  - *Validate:* `python -m pytest tests/test_providers.py -q`.
- [x] **T0.7 — Redact embedding inputs (security).** `embed()` currently posts texts **unredacted** (`providers.py`); route embedding inputs through the redaction guard (strict-block on a hit) before they leave the machine — closes the "never send secrets to any AI model" gap (§12) **ahead of P5.2**.
  - *Validate:* `tests/test_providers.py::test_embed_redacts` — a planted secret never appears in the captured `/embeddings` request body.
- [x] **T0.8 — Deterministic `verify --dry-run` (config-only).** Add a **no-network** mode to `app.cli.verify` that checks config + role→provider mapping + token presence **without calling GitHub**; the **live** completion check becomes explicit opt-in (`--live`, manual smoke only). Keeps CI/test runs offline (open decision #5).
  - *Validate:* `python -m app.cli.verify --dry-run` exits 0 with a stubbed env; `tests/test_verify_dryrun.py` (no network).
- ✅ **Checkpoint P0** — baseline green: tests + lint + `verify --dry-run` pass with **no network**; the live `--live` smoke is optional/manual.

---

## P1 — Storage & data model (§9)

> Goal: a deterministic SQLite layer that all later phases persist into. Pure code, no AI.

- [x] **T1.1 — DB connection + pragmas.** `app/storage/db.py`: open SQLite (WAL, foreign_keys ON), a `connect(path)` + in-memory mode for tests.
  - *Validate:* `tests/test_db.py` opens an in-memory DB and reads `PRAGMA foreign_keys`.
- [x] **T1.2 — Migration runner.** Tiny forward-only migration mechanism (`app/storage/migrations/` + `migrate(conn)`); records applied versions in a `schema_migrations` table.
  - *Validate:* `tests/test_migrations.py` runs twice → idempotent.
- [x] **T1.3 — Identity tables.** Migration `0001`: `users`, `user_identities`, `user_traits`, `sessions`, `messages` (per §9).
  - *Validate:* test creates schema + inserts/selects an owner user.
- [x] **T1.4 — Request/job tables.** Migration `0002`: `requests` (code `YYYYMMDDHHmmSS[-NN]`), `request_details`, `jobs`.
  - *Validate:* test round-trips a request → job link.
- [x] **T1.5 — Plan tables.** Migration `0003`: `plans`, `phases`, `plan_tasks` (recursive `parent_task_id`), `steps`.
  - *Validate:* test inserts a plan→phase→task tree + a step.
- [x] **T1.6 — Role/audit tables.** Migration `0004`: `agents`, `role_messages` (envelope log), `ai_calls`, `audit_log`.
  - *Validate:* test inserts a `role_messages` row with `causation_id` chain.
- [x] **T1.7 — Memory tables.** Migration `0005`: `memories` (state active/archived/dropped + tombstone fields), `memory_tags`, `memory_archive`, `final_reports`, `library_index`, `embeddings`, `artifacts`.
  - *Validate:* test inserts a memory + tag + final_report.
- [x] **T1.8 — Schedule/report tables.** Migration `0006`: `user_interests`, `schedules`, `reports`.
  - *Validate:* test round-trips a schedule row.
- [x] **T1.9 — Repository: requests/jobs.** `app/storage/repos/requests.py` — typed create/get/list + `code` generator with the `-NN` same-second tiebreak (canonical id).
  - *Validate:* `tests/test_requests_repo.py` incl. two same-second requests get `…` and `…-01`.
- [x] **T1.10 — Repository: memories.** `app/storage/repos/memories.py` — create/get/search-stub/update-state, incl. the **drop = delete hot rows + keep thin tombstone** rule (§9.1).
  - *Validate:* test drops a memory → tombstone row remains, `superseded_by` still followable.
- [x] **T1.11 — `db` CLI.** `app/cli/db.py` to create/inspect the schema (`--schema`, `--migrate`).
  - *Validate:* `python -m app.cli.db --migrate` then `--schema` prints tables.
- ✅ **Checkpoint P1** — schema + repos round-trip; recovery story (folders+DB) viable.

---

## P2 — Skills as APIs (§8)

> Goal: the deterministic skill boundary — the only place "actions" run.

- [x] **T2.1 — `SkillSpec` + `@skill` registry.** `app/skills/registry.py` (name→spec, JSON-Schema from pydantic, duplicate guard, `catalog()`).
  - *Validate:* `tests/test_registry.py` registers a dummy skill + reads its catalog entry.
- [x] **T2.2 — `SkillContext`.** `app/skills/context.py` — deterministic services only (db, config, user_id, logger); explicitly **no model**.
  - *Validate:* test constructs a context with a fake db.
- [x] **T2.3 — Policy gate (permission scope + effect class + confirmation rule).** `app/skills/policy.py` — each skill declares a **permission scope** + an **effect class** (`read` | `local_write` | `external`); confirmation is required **only** for `external` / user-visible effects, while `local_write` (`memory.write`, `memory.tag`, `profile.update`, reinforcement touches) is **permission-gated but not user-confirmed** (§9.1) — avoids over-prompting on local DB writes. *(Generalizes the spec's `side_effects: bool` into an effect class.)*
  - *Validate:* `tests/test_skill_policy.py` — `read` & `local_write` skip confirmation; `external` requires it; a missing permission is rejected.
- [x] **T2.4 — Runtime `execute()`.** `app/skills/runtime.py` — validate params → policy gate → run → record `steps` row (the §8.6 pipeline). Reject unknown skill.
  - *Validate:* `tests/test_runtime.py` — happy path records a step; bad params rejected; unknown skill raises.
- [x] **T2.5 — First read-only skill: `memory.search`.** Wire to the P1 memories repo (stub ranking for now).
  - *Validate:* test runs it through `execute()` and gets hits.
- [x] **T2.6 — `memory.get` + `library.read` (reinforcement).** Read skills that refresh TTL/weight (§9.1) — the touch is emitted, applied immediately.
  - *Validate:* test asserts `last_used_at`/`use_count`/`expires_at` advance on read.
- [x] **T2.7 — `memory.write` + `memory.tag`.** Local DB writes (no network).
  - *Validate:* test writes + tags a memory, normalized tag stored.
- [x] **T2.8 — Skill auto-discovery.** `app/skills/__init__.py` imports submodules so `@skill` runs at import.
  - *Validate:* test asserts the catalog contains the registered skills after `import app.skills`.
- ✅ **Checkpoint P2** — catalog + runtime enforce validate→gate→run→record.

---

## P3 — AI advisor wrapper (§7)

> Goal: every model call renders a versioned template, validates output into a schema, and audits the call. Tested with a **fake provider** (no network).

- [x] **T3.1 — Fake provider for tests.** `tests/fakes.py` — a deterministic `AIProvider` returning canned JSON.
  - *Validate:* `tests/test_fakes.py` sanity check.
- [x] **T3.2 — Template loader.** `app/advisor/templates.py` + `config/templates/` (`<role>.<action>.md` + `.schema.json`), version-pinned (§6D).
  - *Validate:* test renders a template with variables + loads its schema.
- [x] **T3.3 — Advisor wrapper core.** `app/advisor/wrapper.py` — render → call provider(role) → parse into pydantic → write `ai_calls` row. No repair yet.
  - *Validate:* `tests/test_wrapper.py` with the fake provider returns a validated object + an `ai_calls` row.
- [x] **T3.4 — Bounded repair/retry + fallback.** On schema failure: one repair attempt → deterministic fallback/escalate; record `validation_status`.
  - *Validate:* test feeds malformed-then-valid JSON → repaired; always-malformed → fallback.
- [x] **T3.5 — Redaction on the wrapper path.** Assert outbound prompt content passes the redaction guard (already in provider, assert at wrapper boundary too).
  - *Validate:* test injects a fake secret → never present in the captured request.
- [x] **T3.6 — `Advisor.triage` (first typed method).** Returns `Triage{kind, clarity, complexity, confidence, rationale}` via template `triage.classify`.
  - *Validate:* test: fake provider → valid `Triage`.
- [x] **T3.7 — `Advisor.analyze` (analyzer contract).** Template `analyzer.analyze` → validated `Analysis{belongs, kind, clarity, complexity, confidence, rationale, plan?, clarify?}` (§6D). *(Required by P4.4 — without it the first CLI ask can't satisfy the role contract.)*
  - *Validate:* test: fake provider → valid `Analysis`; malformed → repair then fallback.
- [x] **T3.8 — `Advisor.answer` (junior contract).** Template `junior.answer` → validated `AnswerDraft{answer, citations:[Source], confidence}` (§6D). *(Required by P4.5; the answer must carry ≥1 citation.)*
  - *Validate:* test: fake provider → valid `AnswerDraft`; a zero-citation answer is rejected.
- ✅ **Checkpoint P3** — AI output is schema-validated + audited; the `triage` / `analyze` / `answer` contracts P4 needs all exist; provider swappable.

---

## P4 — Roles, envelope & control loop (§6, §6A, §6D) — **first end-to-end**

> Goal: answer a **simple ask** end-to-end through PM → Boss → Analyzer/Junior Worker, all via typed envelopes, driven from a CLI (no Telegram yet). Single hardcoded owner user.

- [x] **T4.1 — `RoleMessage` envelope + `Action` enum.** `app/roles/envelope.py` (pydantic) + persistence via the P1 `role_messages` repo.
  - *Validate:* `tests/test_envelope.py` round-trips an envelope; `action` is constrained.
- [x] **T4.2 — Boss router skeleton.** `app/roles/boss.py` — maps an inbound `*_done`/`route_request` to the next verb (deterministic table). No real work yet.
  - *Validate:* test: given `analysis_done{plan_ready}` → schedules `review_plan`.
- [x] **T4.3 — PM first-pass routing.** `app/roles/pm.py` — wrap inbound into a `RequestCard`, auto-assign `/req` id, empty-queue=new, else best-guess (§6C). Emits `route_request`.
  - *Validate:* `tests/test_pm_routing.py` — empty queue mints new; explicit `/req <id>` appends.
- [x] **T4.4 — Analyzer validation + triage.** `app/roles/analyzer.py` — confirm append vs reject (≤`max_append_reroutes`), classify kind (uses `Advisor.analyze`, T3.7). For an **ask**, route to Junior Worker.
  - *Validate:* test: wrong append rejected once → reroute; clear ask → `answer_ask`.
- [x] **T4.5 — Junior Worker (ask path).** `app/roles/junior.py` — run `memory.search` (+ stub web later) → draft validated answer with citations (uses `Advisor.answer`, T3.8) → `ask_done`.
  - *Validate:* test: ask → validated `AnswerDraft`.
- [x] **T4.6 — Control loop + `ask` CLI.** `app/cli/ask.py` drives one request through the company roles synchronously and prints the answer.
  - *Validate:* `python -m app.cli.ask "hello"` returns a validated answer; `tests/test_ask_e2e.py` with fake provider.
- [x] **T4.7 — Persist the full trace.** Ensure the run writes `requests`/`jobs`/`steps`/`role_messages`/`ai_calls` rows; add a recovery test (rebuild state from DB).
  - *Validate:* test asserts the expected rows + a restart re-reads them.
- ✅ **Checkpoint P4** — **simple ask works end-to-end, fully audited & recoverable.**

---

## P5 — Memory & library (§9, §9.1, §9.2)

> Goal: real hybrid search + TTL lifecycle + on-disk library with final reports.

- [x] **T5.1 — FTS5 keyword search.** `*_fts` mirrors + `MemoryService.keyword_search`.
  - *Validate:* test indexes 3 memories, queries, ranks.
- [x] **T5.2 — Embeddings + vector search (pure-Python).** Wire `embedder` role; store/query vectors (embedding inputs already pass the redaction guard — T0.7). *(Decided: pure-Python brute-force cosine over the `embeddings` blob — no `sqlite-vec` dependency; offline + hash-pinned. The `vector_search` surface lets a `sqlite-vec` backend swap in later — Open decision #3.)*
  - *Validate:* test embeds via fake embedder + nearest-neighbor returns expected id.
- [x] **T5.3 — Hybrid ranking (RRF).** Merge FTS + vector deterministically.
  - *Validate:* test: hybrid beats either alone on a planted example.
- [x] **T5.4 — Effective-weight + TTL fields.** Implement `w_eff` formula + `expires_at` on write (§9.1).
  - *Validate:* `tests/test_weight.py` checks decay + reinforcement math.
- [x] **T5.5 — Reinforcement on use/read.** Hook reads (T2.6) + validated-answer use to slide TTL.
  - *Validate:* test: read extends `expires_at` past prior value; revive archived→active.
- [x] **T5.6 — Daily sweep job (expire/decay/archive/consolidate/drop).** `app/memory/sweep.py`, deterministic; drop = delete hot rows + tombstone + move to `index.dropped.json`.
  - *Validate:* `tests/test_sweep.py` over a seeded set → expected state transitions.
- [x] **T5.7 — Folder library + index files.** `app/memory/library.py` — `data/library/Active/{Simple,Tasks,Features}/<id>/`, `index.json`, `index.dropped.json`.
  - *Validate:* test writes an ask folder + updates `index.json`.
- [x] **T5.8 — Final report assembly + Librarian commit.** Build the §9.2 JSON, validate, single-writer commit to `final_reports`+`library_index`+folder.
  - *Validate:* test: ask run produces a committed final report on disk + DB.
- [x] **T5.9 — Archive compaction.** Zip artifacts-except-final_report on cold; revive/unzip on read.
  - *Validate:* test: archive → `artifacts.zip` present, `final_report.md` readable; read revives.
- ✅ **Checkpoint P5** — memory stays small, fresh, recoverable; asks archived.

---

## P6 — Complex jobs: plan → phase → task (§6B)

> Goal: a **task** job runs a plan with phases, sign-off, and a final report — **and a feature job** additionally emits gated generated code/skills (§5). Concurrency capped at `max_concurrent_jobs`.

- [x] **T6.1 — Analyzer plan drafting.** Produce a validated `PlanDraft` (phases→tasks, deps) via the advisor.
  - *Validate:* test: complex request → schema-valid plan.
- [x] **T6.2 — Status state machine.** `app/roles/lifecycle.py` — New→Approved→Active→InProgress→Resolved→Closed (+Abandoned) per entity; who-sets-what enforced.
  - *Validate:* `tests/test_lifecycle.py` — legal transitions pass, illegal rejected.
- [x] **T6.3 — Company Expert sign-off.** Approve/decline plans + phases (decline ≤`max_phase_declines`, then escalate).
  - *Validate:* test: decline path loops then escalates at the cap.
- [x] **T6.4 — Boss scheduling + per-job runner.** Start a per-job runner as an **`asyncio` task** (decided — Open decision #2) grouping the execution roles; the Boss starts it on approval and **disposes** it after archive; enforce the `max_concurrent_jobs` cap, queue extras. Logical isolation only (own `JobContext` + folder + inbox); **no separate OS process**.
  - *Validate:* test: 4 complex jobs → 3 run, 1 queued.
- [x] **T6.5 — Senior Worker task execution.** Run tasks (respect deps, serial/parallel) through the skill runtime; warm-session checkpoint to folder+DB.
  - *Validate:* test: dependent tasks run in order; results recorded.
- [x] **T6.6 — Plan Expert phase/final reports.** Phase resolution + assemble final report; submit to Company Expert.
  - *Validate:* test: all tasks resolved → phase Resolved → report.
- [x] **T6.7 — Pause/resume/abandon.** **Pause:** `jobs.paused` (DB) is the durable truth + a per-job `asyncio.Event` is the live signal; the Boss stops scheduling and the runner checkpoints + parks at the next step boundary (no flag file). Pause holds the slot (named tradeoff, §6B); resume re-evaluates the plan. **Abandon:** `task.cancel()` → `CancelledError`; a `try/finally` marks plan/phases/tasks `Abandoned` and frees the slot.
  - *Validate:* test: pause checkpoints + holds slot + parks on the event; resume continues; abandon cancels, marks `Abandoned`, frees the slot.
- [x] **T6.8 — Improvement loop.** On finish: **archive+close original on both branches**, then optionally spawn a linked improvement request (`improves_request_id`), capped by `max_improvement_iterations`.
  - *Validate:* test: confirm-improvement spawns a linked request *after* the original is Closed; decline just closes.
- [x] **T6.9 — Feature-job deliverable: inert generated code/skills.** A **feature** job writes proposed skills/code into `backend/app/skills/generated/<job>/`, **inert** — not registered or executed — until confirmed (§5, §6B).
  - *Validate:* `tests/test_generated_inert.py` — a feature plan writes generated code; it is **absent from the live catalog** and never executed.
- [x] **T6.10 — Generated-code review → confirm → activate.** Plan Expert review + user confirmation (`confirm_generated_code: true`) flips a generated skill to **active/registered**; a decline leaves it inert.
  - *Validate:* test: confirm → skill now in catalog + runnable through the runtime; decline → stays inert.
- ✅ **Checkpoint P6** — task **and** feature jobs run with sign-off, concurrency, recovery; generated code is gated.

---

## P7 — Auth (device flow) & owner pairing (§7.2, §10.1)

> Goal: Route A login + bind the chat bot to a single owner; reject everyone else.

- [x] **T7.1 — Device-flow auth core.** `app/advisor/auth.py` — device code → poll → `gho_` → exchange `copilot_internal/v2/token` → cache+refresh (modeled on Hermes). Token cache `data/.auth/github.json` (git-ignored, perms 600).
  - *Validate:* `tests/test_auth.py` against mocked GitHub endpoints (no network); cache never in DB/logs.
- [x] **T7.2 — `GitHubCopilotProvider` (Route A).** Add the provider kind; `build_provider` selects it; asks `auth` for the bearer.
  - *Validate:* test: provider builds + sets Copilot headers (mocked transport).
- [x] **T7.3 — `login` CLI.** `app/cli/login.py` runs the device flow, prints the user code.
  - *Validate:* manual smoke documented; unit test drives the flow with mocked endpoints.
- [x] **T7.4 — Owner identity + allowlist check.** Gateway helper: resolve `(channel, channel_user_id)` → only `paired` admitted; refusals audited + rate-limited (§10.1).
  - *Validate:* `tests/test_allowlist.py` — paired passes, unpaired refused + audited.
- [x] **T7.5 — `pair` CLI + device-flow challenge.** `app/cli/pair.py` — mint/list/revoke one-time codes + the owner device-flow challenge; writes `user_identities` binding.
  - *Validate:* test: pair via host code binds owner; non-owner login refused.
- ✅ **Checkpoint P7** — only the paired owner can drive the system.

---

## P8 — Channels: Telegram first (§10)

> Goal: a real chat message in, a validated answer out — through the allowlist.

- [x] **T8.1 — `ChannelAdapter` protocol + canonical messages.** `app/channels/adapter.py` (`InboundMessage`/`OutboundMessage`, `parse_inbound`/`send`/`verify`).
  - *Validate:* test: a raw payload → canonical `InboundMessage`.
- [x] **T8.2 — Telegram adapter (parse + send).** `app/channels/telegram.py` — Bot API; signature/secret verify.
  - *Validate:* test with a recorded Telegram update fixture (no network).
- [x] **T8.3 — Gateway ingress wiring.** Connect adapter → allowlist (P7) → PM control loop (P4). Long-poll runner (`app/cli/telegram.py`).
  - *Validate:* test: fake inbound from a paired user → answer enqueued; unpaired → refused.
- [x] **T8.4 — `pair` over chat (`/pair <code>` + unpaired hint).** The chat side of host-code pairing: `/pair <code>` binds the sender to the owner; an unpaired non-`/pair` sender gets a single "pair first" hint (rate-limited, audited).
  - *Validate:* `tests/test_ingress.py` — `/pair <valid>` binds + confirms; unpaired sender refused with a hint, no request created.
- ✅ **Checkpoint P8** — Telegram round-trip works for the owner only.

### P8.5 — Chat-app onboarding: connect the bot + request-and-approve pairing — *planned, needs decisions (open #8)*

> Two **separate** concerns (don't conflate them):
>
> **A. Connect the bot to the chat service** *(operator-only, once).* How this works is **platform-specific**:
> - **Bot-token platforms (Telegram, Discord, Slack, Teams):** register a bot with the platform (Telegram **@BotFather**) → paste the **token**. There is **no QR** here — the bot is a first-class app identity. *(This is what's built today, T8.2/T9.4.)*
> - **Linked-device platforms (WhatsApp; also a Telegram *user* account via MTProto):** the **chat service issues a QR** through its session/protocol; we **render that QR in the console** and the owner scans it with their phone app's "linked devices" to approve. The QR content comes **from the chat service**, not from us — we only render + display it. *(Needs a per-platform session library; out of scope until we target such a platform — see open #8.)*
>
> **B. Pair a user (request-and-approve).** Once the bot is connected, authorizing *who* may chat is **user-initiated, host-approved** (the operator approves on the trusted console — the model never decides):
> 1. An **unpaired** user messages the bot.
> 2. The bot creates a **pending pairing request** for that `(channel, channel_user_id)` and **replies with a short pairing code** (e.g. `ABCD-1234`), telling the user to ask the operator to approve it.
> 3. The operator runs **`python -m app.cli.pair --list`** to see pending requests (code + channel + user), then **`--approve <code>`** to bind that account to the owner.
> 4. The bot **confirms** to the user on their next message (or proactively). Refusals before approval stay rate-limited + audited (§10.1).
>
> *Reference note:* the GitHub **device-flow** auth (§7.2) is the part modeled on OpenClaw/Hermes; the chat **pairing** flow above is a standard request-and-approve pattern (not something those repos implement — they're coding CLIs, not chat bots).

- [x] **T8.5 — Pending pairing requests (schema + repo).** A `pairing_requests` row per unpaired `(channel, channel_user_id)`: a short code, `pending|approved|expired`, TTL, created_at. Repo: `create_or_refresh(channel, user) -> request`, `get_request`, `list_pending`, `approve(code)`, `expire_stale`. One pending row per account (a repeat message reuses the code); the code is a **claim ticket** (stored plaintext — possession grants nothing without console approval).
  - *Validate:* `tests/test_pairing_requests.py` — first unpaired message creates a request + code; repeat reuses the same pending code; approve marks approved; expired/used/unknown codes rejected.
- [x] **T8.6 — Ingress: reply-with-code for unpaired senders.** An unpaired sender gets a **pairing code** (created via T8.5) in the reply — "Your pairing code is `ABCD-1234`. Ask the operator to run `pair --approve ABCD-1234`." Still **no request/job created**, rate-limited + audited; a repeat reuses the same code (no spam). A `/pair <code>` typed in chat remains supported (host-minted codes).
  - *Validate:* `tests/test_ingress.py` — an unpaired sender's reply contains a fresh code + a pending request exists; a second message reuses the same code; after `approve_pairing_request` the sender is admitted and answered.
- [x] **T8.7 — `pair --list` / `--approve` (console approval).** `app/cli/pair.py`: `--list` shows pending requests (code, channel, user, age) alongside active host codes + paired accounts; `--approve <code>` binds the requesting account to the owner. Keeps `--revoke`/`--mint`/`--challenge`.
  - *Validate:* `tests/test_pair_cli.py` — a seeded pending request is listed; `--approve <code>` flips it to paired (allowlist now admits it); an unknown code is rejected.
- *(On hold — concern **A** QR-connect: `app/channels/qr.py` console QR renderer for linked-device platforms (WhatsApp etc.). Not needed for Telegram (token-based); revisit if such a platform is targeted — open #8.)*

---

## P9 — First-run setup & onboarding wizard (§7.2, §10, §13)

> Goal: one guided command takes an **already-checked-out repo** to a **configured, verified, runnable** app — pick + authenticate the AI provider, set the Telegram bot token, and establish + pair the owner — **without hand-editing `.env` or `config/models.yaml`**. The wizard **orchestrates the pieces that already exist** (device-flow `login`, `pair`, `verify`, the Telegram adapter); it adds no new control path. It does **no git operations** (no `git clone`/`pull`) and touches **no source files** — it only writes config (`.env`, `config/models.yaml`) + the auth cache; getting the code is the user's `git clone`, out of scope here. Every step is **idempotent + re-runnable** and **unit-tested offline** with injected I/O — no real prompts, no network, no secrets in tests/logs (§12).
>
> **Skip what already works (don't clobber).** Each step first **detects existing configuration** and, when present + usable, **skips by default** — the wizard never overwrites a working `.env` value, provider route, cached login, or owner pairing unless the user asks. The default run only fills **gaps**; it reports each step as `configured (kept)` / `set up now` / `still missing`. Overriding is explicit: `--reconfigure[=step]` re-asks (and only then may replace), and a detect-only `--check` changes nothing. This keeps re-runs safe on a machine that's already partly set up.

- [x] **T9.1 — `.env` + `models.yaml` config writer (pure, idempotent).** `app/setup/config_writer.py` — read/merge/write `.env` **preserving existing keys, comments, and order** (never duplicating a key), and select the `config/models.yaml` provider route (Route A `github_copilot` vs Route B `github_models`). No prompts, no network — just the deterministic file surface the wizard calls.
  - *Validate:* `tests/test_setup_config_writer.py` — writing a key round-trips; unrelated keys/comments preserved; re-writing an existing key updates it **in place** (idempotent, no duplicate).
- [x] **T9.2 — Interactive prompt helpers (injectable I/O).** `app/setup/prompts.py` — `ask` / `confirm` / `secret` helpers with **injectable** input/output streams so the wizard is fully scriptable in tests; secret inputs are **never echoed** and never logged (§12). Each helper takes a **current value**: when one already exists it's shown (secrets **masked**) and offered as the default, so pressing Enter **keeps** it.
  - *Validate:* test drives each helper with scripted input; a secret answer never appears in captured output; an empty answer keeps the supplied current value.
- [x] **T9.3 — AI-provider setup step.** Choose **Route A** — GitHub Copilot **device-flow login** (reuse `auth.py` / `app.cli.login`, the OpenClaw/Hermes flow: print the `user_code` + `https://github.com/login/device`, poll, then confirm `get_bearer()` works) — or **Route B** — a GitHub Models **PAT** captured into `.env`. Write the matching `models.yaml` route (and keep the `embedder` on `github_models`, since Copilot has no embeddings). **Skip when already usable:** if a cached Copilot login (`is_logged_in()`) or a present PAT already satisfies the configured route, report `configured (kept)` and don't re-auth unless `--reconfigure`.
  - *Validate:* `tests/test_setup_provider.py` — Route A drives the device flow against **mocked** GitHub endpoints (no network) and writes the `github_copilot` route; Route B stores the PAT + writes the `github_models` route; an **already-logged-in / token-present** run skips auth (no device-flow call) and leaves config unchanged.
- [x] **T9.4 — Telegram setup step.** Capture `TELEGRAM_BOT_TOKEN` (from **@BotFather**) into `.env`; optionally verify it with a `getMe` Bot API call (skippable so setup stays offline). Briefly explain creating the bot + starting a chat. **Skip when already set:** an existing `TELEGRAM_BOT_TOKEN` is kept by default (offer to re-verify, not to re-enter) unless `--reconfigure`.
  - *Validate:* test with a **mocked** Bot API — token captured + written; `getMe` success **and** failure handled cleanly; a `--skip-verify`/offline path writes without any call; an **existing-token** run keeps it (no overwrite).
- [x] **T9.5 — Owner pairing step (device-flow bootstrap).** Establish the **owner** (§10.1): run the device-flow **owner challenge** (`pair.run_device_flow_challenge`, `bootstrap=True`) to bind the owner GitHub login, **and/or** mint a host one-time code for the chat `/pair <code>` flow. Afterward the Telegram allowlist admits only the owner. **Skip when already established:** if an owner login is already set (DB or `OWNER_GITHUB_LOGIN`), report `configured (kept)` and offer only to **mint a fresh pairing code**, not to re-bootstrap, unless `--reconfigure`.
  - *Validate:* test binds the owner via the **mocked** device flow; minting a host code lists it; an **owner-already-established** run skips the challenge.
- [x] **T9.6 — `setup` wizard command + `--check`.** `python -m app.cli.setup` runs the steps in order, **idempotent + skip-existing by default** (a step that detects working config reports `configured (kept)` and is skipped; only **missing** steps prompt), and ends by calling `verify --dry-run`. `--reconfigure[=step]` forces a re-ask of all/one step (the only way to replace working config); `--check` only **reports** what is configured vs missing (no changes, no network).
  - *Validate:* `tests/test_setup.py` — a full wizard run with **all** I/O + network mocked writes `.env` + `models.yaml` and ends green on `verify --dry-run`; a **re-run with everything already configured makes no changes + asks nothing** (pure skip); `--reconfigure` re-asks; `--check` reports status without writing.
- [x] **T9.7 — `scripts/setup.sh` (env + app, one command).** A thin wrapper, run **from inside an existing checkout**: it runs `scripts/setup-env.sh` (venv + pinned deps) then `python -m app.cli.setup` — so once the repo is cloned, a single command reaches a runnable app. It performs **no git operations** (cloning the repo is a prerequisite, not setup's job). The Python wizard (T9.1–T9.6) carries the real test coverage; the shell wrapper stays logic-light (a documented smoke).
  - *Validate:* documented smoke — `./scripts/setup.sh` in an existing checkout reaches the wizard; no `git` invocation; no new untested logic in shell.
- ✅ **Checkpoint P9** — an existing checkout reaches **configured + verified** via one guided `setup` command; no hand-editing of `.env` / `models.yaml`, and no git operations.

---

## P10 — Web app: FastAPI + dashboard (§11)

> Goal: read-only-first dashboard surfacing live state and generated data.

- [ ] **T10.1 — FastAPI app + health.** `app/web/main.py` over the same repos/services; `/healthz`.
  - *Validate:* `tests/test_web_health.py` via `TestClient`.
- [ ] **T10.2 — Requests API + page.** Live job→plan→phase→task tree + steps/ai_calls.
  - *Validate:* test: seeded request renders via the API.
- [ ] **T10.3 — System API.** CPU/mem/disk (host-metrics service) + model usage aggregated from `ai_calls`.
  - *Validate:* test: usage aggregation matches seeded `ai_calls`.
- [ ] **T10.4 — Reports & Data Products API + Refresh-now (manual path).** List products + run history; `Refresh now` re-invokes generator skills via code (the **manual** refresh path; scheduled firing is T10.7).
  - *Validate:* test: refresh triggers a deterministic run + a new `reports` row.
- [ ] **T10.5 — Settings: paired accounts.** Pair/revoke from the web (calls P7).
  - *Validate:* test: revoke flips `user_identities.state`.
- [ ] **T10.6 — Minimal React/Vite shell (optional).** Lightweight dashboard hitting the APIs. *(Confirm scope at review.)*
  - *Validate:* build succeeds; one page renders seeded data.
- [ ] **T10.7 — Scheduler runner (scheduled auto-refresh).** `app/scheduler/runner.py` — find **due** `schedules` (cron / `next_run_at`), fire each as a **normal request/job** (§11.1), advance `last_run_at`/`next_run_at`, and write a `reports` row per run. This is the **scheduled** path; T10.4 is the **manual** path (same generator skills).
  - *Validate:* `tests/test_scheduler.py` — a due schedule fires once, advances `next_run_at`, writes a `reports` row; a not-yet-due one does **not** fire.
- ✅ **Checkpoint P10** — dashboard shows requests, system, and data products; products refresh both **on schedule** and **on demand**.

---

## Cross-cutting (apply within the relevant phase, not as big-bang)
- **Schedules & proactive data products (§11.1):** land the `schedules` table in P1, the generator-skill pipeline in P5/P6, and the scheduler trigger + `Refresh now` in P10.
- **Template-requirement validation (anti-hallucination):** every AI-facing role — **explicitly including the experts (Company Expert, Plan Expert)** — must validate the model reply against its template's declared response schema (required fields, correct types, **no extra/invented fields**) before using it; a reply that doesn't meet the requirement is repaired or escalated, never acted on (§6D, §7). Enforced centrally by the advisor wrapper (P3) and inherited by every role that calls it (P4, P6).
- **Cited-URL existence verification (anti-hallucination):** any **URL** carried in an answer's citations is **deterministically verified to exist** (resolved + reachable) before the answer is accepted — the experts access the cited URLs to confirm they're real; a fabricated/unreachable URL fails validation (repair → escalate). The check is **SSRF-guarded** (only `http`/`https`; rejects private/loopback/link-local/reserved targets) since the URLs are AI-proposed (§6D, §7.1). Centralized in the advisor's answer path (P3) and reused by the experts' report review (P6). **Configurable via the `verify_citation_urls` policy knob (default `true`):** our deterministic fetch can hit **false negatives** where anti-crawler defenses (CAPTCHA, JS/bot challenges, paywalls) block a *real* page an AI/browser could open, so the check can be disabled in `config/policies.yaml`; to be **hardened later** (search-API cross-check / headless render) rather than left strict-only.
- **Audit everywhere:** every AI call → `ai_calls`; every skill → `steps`; every routing hop → `role_messages` (built into P2–P4).
- **Recovery tests:** each stateful phase (P1, P4, P5, P6) includes a "rebuild from folders+DB after restart" test.

---

## Open decisions to confirm during review
1. **Models config shape — decide before P3.** Keep the current single `config/models.yaml` (roles+providers), or migrate to the spec's `config/models/` folder + `model-bindings.yaml` (§7.0)? This **blocks P3** (templates, per-agent-role overrides, provider selection all key off it) and churns later if changed mid-stream. *(Recommend: keep the single file through P6; add `model-bindings.yaml` only when a per-agent-role override is actually needed.)*
2. **Per-job concurrency mechanism — DECIDED (2026-06-15): in-process `asyncio` runner, not a child process.** The work is I/O-bound, the durable truth is already folders + DB, and pause/resume/abandon + `/req` status sharing are far simpler in one address space; `asyncio` `cancel()` also gives clean cooperative abandon that OS threads can't. Isolation is **logical** (own `JobContext` + folder + inbox); a CPU-bound *skill* can be offloaded to a `ProcessPoolExecutor` if ever needed. Spec §6A/§6B updated to match. *(Revisit only if hard fault isolation / force-kill becomes necessary; the recovery contract makes that promotion cheap.)*
3. **`sqlite-vec` dependency** for vectors (P5.2) — **DECIDED (2026-06-15): pure-Python first, no `sqlite-vec`.** Vectors are stored as float32 blobs in the existing `embeddings` table and searched with deterministic brute-force cosine (`app/memory/vectors.py`), so the suite stays fully offline + hash-pinned with **no new dependency**. The `vector_search` surface is deliberately small so a `sqlite-vec` (ANN) backend can replace the internals later — revisit only when the hot set grows large enough that brute-force latency matters.
4. **Web frontend scope** (P10.6) — full React/Vite now, or REST + a minimal HTML page until later?
5. **Test doubles for GitHub/Bing/Telegram** — confirm we mock all external HTTP in unit tests (no live calls in CI). *(Recommend: yes.)* **Implemented (2026-06-15):** the default `pytest` run is fully offline (a `conftest` guard blocks real sockets); a separate **opt-in** suite marked `integration` (`tests/test_live_integration.py`) calls a **real** model but is **deselected by default** (`addopts = -m 'not integration'`) and **skips** without a configured token — so CI stays offline while `python -m pytest -m integration` exercises a live model on demand.
6. **Phase ordering** — OK to defer auth/pairing (P7) until after the CLI core (P4–P6), or do you want pairing earlier?
7. **Cited-URL verification strictness — default ON (2026-06-15).** The deterministic existence check ships **enabled** (`verify_citation_urls: true`), but our fetcher can false-negative on pages guarded by **anti-crawler defenses** (CAPTCHA, JS/bot challenges, paywalls, geofencing) that an AI/browser could open — so it's **disable-able in `config/policies.yaml`**. Planned **hardening (later phase, alongside `web.fetch`):** treat ambiguous/blocked responses as a soft-pass, cross-check existence via the search API, and/or render headless before deciding; revisit whether the default should stay on. *(Open: which hardening lands first, and does the default flip once it's robust?)*
8. **Chat-app onboarding model (P8.5).** Two parts. **(a) Connecting the bot** is platform-specific: **Telegram/Discord/Slack** = a **bot token** (no QR); **WhatsApp / Telegram-user-account** = the **chat service issues a QR** we render in console + scan with the phone app (needs a per-platform session library). *(Decide: stay bot-token Telegram-first, or target a linked-device platform like WhatsApp where the scan-to-connect QR applies?)* **(b) Pairing a user** = **request-and-approve**: unpaired user messages the bot → bot replies a **pairing code** → operator runs `pair --approve <code>` on the console. *(Recommend: build (b) now — platform-agnostic; defer (a)'s QR + session library until/if a linked-device platform is chosen, since Telegram bots have no scan-to-connect.)* A console QR renderer (`segno`, pure-Python, hash-pinned) is only needed once (a) targets such a platform.

---

## Suggested first slice (once you approve)
Start **P0.1 → P0.6** (test harness, lint, characterization tests, policies loader, provider test). Small, no new runtime behavior, and it gives every later step something to validate against. We stop at the **P0 checkpoint** for your review before P1.
