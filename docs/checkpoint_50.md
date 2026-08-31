# Checkpoint — 50% Milestone Verification

Date: 2026-08-30. This is a verification pass, not a construction pass — its job was to run the
actual system end to end with real English audio, run every test suite for real, and be adversarial
about what `plan.md`/`claude_context.md` had been claiming versus what demonstrably works. Companion
documents: `claude_context.md` and `plan.md` (both rewritten alongside this file to describe reality
as of now, with a new "Known Gaps and Debt" section each).

**Headline finding: the milestone this checkpoint was asked to verify — a doctor can log in, record
consent, upload English audio, watch it process, and read a real transcript + 8-field note + summary,
with failures surfaced honestly — did not actually hold up under adversarial testing at the start of
this pass, despite `plan.md` showing every relevant phase as substantially complete.** Five real,
previously-undetected defects were found by actually running the system with real audio instead of
short fixtures, all now fixed and verified live (see §2). The single most important thing this
checkpoint surfaces is **not** a percentage — it's that structural completeness (state machines built,
tests passing, phases marked done) had silently diverged from operational correctness (real jobs
reliably completing), and the recorded percentage never priced that gap in. See §6 for the recomputed
number and why it moved.

---

## 1. What was actually run for this checkpoint

Not simulated, not read from prior session notes — executed live during this pass:

- `docker compose up -d` — all 8 services healthy.
- Real audio, twice: `scripts/fixtures/sample_consultation.wav` (a short fixture) end-to-end through
  the browser via Playwright, and `data/processed/primock57/audio/day2_consultation01.wav` (a real
  5m29s PriMock57 consultation recording) through the API directly, watched live over SSE with `curl
  -N` past the 90-second mark to specifically stress-test the timeout bug found in §2.
- `go test -count=1 ./...` (all packages, plain unit tests — no Docker-dependent integration suite this
  pass, see §4's coverage table for why that matters).
- `go vet ./...` and `golangci-lint run ./...` — both clean.
- Every Python workspace member's test suite: `shared` (11 passed), `asr-service` (18 passed, ~110s —
  real faster-whisper/pyannote imports exercised, Groq mocked), `nlp-service` (21 passed), `eval` (64
  passed).
- `frontend`: `npm run lint` (clean), `npm run test` (vitest — was crashing before this pass, see §5),
  `npm run build` (clean), `npm run test:e2e` (Playwright against the live stack, real pipeline).
- Live Postgres queries against the real dev database (not test fixtures) to check `jobs`, `job_stages`,
  `run_configs`, `extractions`, `clinical_notes`, `summaries`, `eval_results` row counts and content.

---

## 2. Bugs found by actually running the system, fixed and verified live this pass

None of these were caught by the existing test suite (§4 explains why for each). All are now fixed,
rebuilt, and re-verified against the live stack.

1. **Worker self-reclaim threshold (5 min default) shorter than real ASR wall-clock (9+ min
   measured).** `coda_worker_sdk.consumer.StreamConsumer`'s periodic reclaim sweep self-reclaimed a
   message still being legitimately handled by the same in-flight task, dispatching a duplicate run
   the instant the original finished — and since real audio routinely exceeds 5 minutes, this recurred
   indefinitely, permanently starving `asr-service` (single-concurrency) of capacity to ever start a
   new job. Fixed: `asr-service`/`nlp-service` now pass `min_idle_ms` matched to
   `go/internal/queue/policy.go`'s real per-stage `VisibilityTimeout` (40 min / 90 min) instead of the
   SDK's generic 5-minute default.
2. **`job_stages` rows keyed by `idempotency_key` alone, with no `job_id` reassignment on conflict.**
   RunConfig interning (ADR-0012) means a *different* job (a resubmit after a consultation's prior job
   dead-lettered without this stage ever succeeding) computes the exact same idempotency key —
   observed live: a resubmitted job inherited its dead-lettered predecessor's exhausted attempt=3
   count as attempt=4, and its own `GET /jobs/{id}` showed an empty `stages` array — the real work was
   progressing, invisibly, against the wrong job's row. Fixed: `DispatchJobStage`'s upsert now
   reassigns `job_id`/resets `attempt`/`started_at`/`error_history` when ownership genuinely changes
   hands, while a true same-job retry is unaffected.
3. **`RunConfig.AsrBackend` hardcoded to `"groq"` in `go-api`, when `asr-service` only ever runs
   faster-whisper (decision #55).** This fed `queue.PolicyOptionsFor(...).LocalASR = false`, giving
   every real ASR job a 5-minute soft deadline instead of the correct 20 minutes for local CPU
   transcription — almost certainly the actual reason jobs were dead-lettering in the first place,
   independent of bug #1. Fixed: `AsrBackend` now says `"faster_whisper_local"`; verified live that a
   fresh dispatch now carries a 20-minute deadline, not 5.
4. **`runConfigContentHash` hashed raw `protojson.Marshal()` output directly**, which Go's protojson
   package does not guarantee is byte-stable across calls for an identical logical message — confirmed
   live: two RunConfigs with identical field values produced different hashes and different interned
   rows on separate calls. This silently defeated ADR-0012's entire content-hash-interning guarantee
   (every reported number reconstructible from a Postgres dump; identical arms sharing one row) for
   every job dispatched through `go-api`'s REST API. Fixed by canonicalizing through `encoding/json`
   (which does sort map keys, per its own documented guarantee) before hashing; verified live that two
   separate dispatches of the same arm now produce the identical `run_config_id`. A new unit test
   (`TestRunConfigContentHash_Deterministic`, 50 repeated calls) guards the regression — there was no
   test at all for this function before this pass.
5. **The actual root cause of the original "SSE doesn't update live" complaint**: `router.go`'s global
   `middleware.Timeout(ServerCfg.RequestTimeout)` (30s default) wraps every route, including
   `GET /jobs/{id}/events`. A child `context.WithTimeout` can only ever produce the *earlier* of two
   deadlines, so `JobEvents`'s own, intentional 30-*minute* `sseMaxDuration` was silently clamped to 30
   *seconds* for every connection. The server then closed the stream cleanly (not an error) once that
   fired, and the client (`lib/sse.ts`) treated any clean close as "the job reached a terminal state,
   nothing more to do" — freezing the UI at the last frame received, forever, with no error shown and
   no reconnect attempted. Since real consultation audio takes minutes, this was not an edge case; it
   was close to guaranteed for any real-length recording. Fixed on both sides: the server strips the
   inherited deadline via `context.WithoutCancel(r.Context())` before applying its own 30-minute one
   (a client disconnect is still detected the ordinary way — a failed `Write`/`Flush`, which the
   handler already checks); the client now treats a clean stream end as a recoverable disconnect and
   reconnects with backoff, the same as an error, unless the caller has already called `close()` after
   observing a genuinely terminal job state. Verified live: a real 5m29s-audio job's SSE connection
   stayed open and kept delivering heartbeats past the 90-second mark under direct observation (would
   previously have died at ~30s).

**Why none of these were caught earlier**: bugs 1, 2 and 5 all require a job whose real processing time
exceeds ~30 seconds to manifest — every prior *passing* demonstration in this project (including the
first working Playwright run recorded in `claude_context.md`) used a 1-second fixture clip specifically
because it was fast, which is exactly what let all three hide. Bug 3 requires inspecting the actual
persisted `run_configs.config` JSON against what `asr-service` really runs — nothing in the test suite
diffs those. Bug 4 requires calling the hashing function twice and comparing — again, nothing did.

---

## 3. `plan.md` claims vs. demonstrable reality, by phase

| Phase | `plan.md` claim before this pass | What's actually demonstrable now |
|---|---|---|
| 0 — Scaffolding | 90%, in-progress | Holds. Clean-clone reproduction genuinely still unverified (no README yet) — the one honest gap already named. |
| 1 — Vertical slice | 50% | Holds structurally, but the ASR half of this phase was the one silently broken by bugs #1/#3/#5 above until this pass. Real, now-working: audio → real transcript with real speaker turns → real 8-field JSON → real summary, verified live end-to-end. Diarization/role-classification now confirmed running live for the first time this session (previously blocked on missing credentials) — still only against short fixture audio, so DER/role-accuracy remains unmeasured on anything realistic. |
| 2 — Data/eval measurement | 35% | Holds. Real WER/CER (0.2226/0.1666) on 8 real PriMock57 items, unchanged this pass. |
| 3 — Go control plane | **100%, done** | **Overstated.** Three of the five bugs in §2 (self-reclaim, job_stages collision, AsrBackend/policy mismatch) live squarely in this phase's own code and were undetected by its own (passing) test suite. The phase's *named* acceptance criteria (state machine, transport, crash recovery, structurally-enforced redaction ordering) do hold as tested — but "100%, done" read as "this works," and until today it did not reliably, for any real audio. Recommend downgrading the confidence label on this phase from "done" to "done, with 3 real defects found and fixed post-hoc" rather than silently leaving "100%" standing unqualified. |
| 4 — Single-pass extraction | 55% | Holds. Real extraction, real repair loop, real Postgres writes — confirmed again this pass via fresh live jobs (64 `extractions` rows, 16 `clinical_notes`, all `status='draft'`, none approved/exported — matches the known gap that review/approve/export isn't built). |
| 5 — Eval harness | 55% | Holds. The specific numbers (F1 0.6612, ROUGE-L 0.3440, BERTScore 0.8531, hallucination 0.2088) were not re-run this pass — re-running would re-spend real Groq quota for numbers already real and already in Postgres (verified again via direct query this pass: 188 `eval_results` rows across 2 distinct `run_config_id`s, consistent with two real eval-harness invocations, not corrupted by bug #4 above — the eval harness computes its own RunConfig in Python, not through `go-api`'s buggy `runConfigContentHash`). |
| 6 — GoT-lite engine | 0% | Holds. Genuinely nothing built — no thought-graph construction, no multi-candidate generation, no scoring/refinement loop exists anywhere in `nlp-service`. This is the core research contribution and it has not started. |
| 7 — Knowledge augmentation | 0% | Holds. No MeSH/ICD-10 linking code exists. |
| 8 — Review UI and export | 40% | Holds, unchanged by this pass — read-only vertical slice real and tested; edit/approve/export genuinely absent. |
| 10 — Ablation runs | 0% | Holds — blocked on Phase 6/7, which don't exist. |
| 11 — Packaging/demo | 0% | Holds. |

---

## 4. Test coverage by module, with the gaps that matter

| Module | Coverage | What would go undetected |
|---|---|---|
| `go/internal/auth` | Solid (5 files) | — |
| `go/internal/http` | Thin for plain `go test ./...` — only `integration_test.go` (Docker-gated) existed before this pass; one new file (`jobs_handlers_test.go`, added this pass) now covers the content-hash determinism and AsrBackend regressions specifically | Any other handler-level bug is invisible without running the Docker-dependent integration suite, which this checkpoint pass did not run (feasible but time-boxed out) |
| `go/internal/queue` | Solid (3 unit + integration) | — |
| `go/internal/pipeline` | Solid (5 unit + 3 integration) | **No test covers cross-job idempotency-key collision** (bug #2) — a regression there would ship silently again |
| `go/internal/tasks` | **Zero tests** | The 3 Asynq periodic jobs (DLQ alerting, stale-job reaping, artifact retention) have no test at all; their error paths are unverified by anything but live observation |
| `go/internal/storage`, `audit` | Thin (1 file each) | — |
| `go/internal/db`, `telemetry` | None (generated code / low-logic) | Acceptable |
| `asr-service` | Thin-solid (5 files) | **No test for real (non-mocked) diarization or the real Groq role-classifier call** — pyannote is exercised only via import-succeeds |
| `nlp-service` | Thin-solid (6 files) | **Zero tests for `_handle_redact` specifically** — `test_worker_nlp.py` only uses `text_redacted` as fixture input for extraction, never asserts anything about the redact handler. A regression that "fixed" it (or broke it differently) would go undetected |
| `shared` (worker SDK) | Thin (3 files) | **No test for `consumer.py`/`StreamConsumer` at all** — the exact module containing bug #1 (self-reclaim). No test exercises reclaim timing or duplicate-dispatch behavior |
| `eval` | Solid (9 files) | — |
| `frontend` | **Effectively none** — `sanity.test.ts` is literally `expect(1+1).toBe(2)`; the one real coverage is the single, slow, full-stack Playwright e2e spec | Zero component/hook unit tests across ~15 source files. Any isolated regression in, e.g., `lib/sse.ts`'s reconnect logic (which this pass rewrote) is only caught by a full multi-minute live-stack run, not a fast unit test |

**Doc consistency**: `docs/architecture.md`'s own header banner still says "the Python worker side of §2
and §6 are not built" — stale; both have been real and running since well before this pass. No
claude_context.md decision claims to have updated that banner; it was just never touched as phases
progressed. Fixed as part of this pass's doc rewrite.

---

## 5. Stubbed, mocked, partial, or silently-failing features

Ranked by how much they matter:

1. **`STAGE_REDACT` is a pure echo — no real PII redaction exists.** `nlp_service/worker.py`'s
   `_handle_redact` does exactly `turn.text_redacted = turn.text` for every turn — no pattern
   matching, no NER, nothing. `extraction.py`/`summary.py` correctly read only `turn.text_redacted`
   (the letter of ADR-0014 is followed), but since that field is an unredacted copy, **real,
   unredacted PII currently reaches Groq, a third-party LLM provider, on every extraction and summary
   call.** ADR-0014 promised pattern-and-NER-based detection with reversible, encrypted-mapped
   placeholders; none of that exists — only the structural half (the pipeline state is mandatory and
   unskippable) is real. This is disclosed honestly in the code's own comments and in
   `claude_context.md` (never claimed as done), but it is the single most consequential gap in the
   system as it stands today — a genuine compliance-relevant gap, not a nice-to-have. **Not fixed this
   pass** — building real redaction is a real subsystem (pattern/NER detection, placeholder mapping,
   encrypted reversibility), squarely out of this checkpoint's explicit milestone scope, not a cheap
   fix. See §7's Known Gaps for the recommended next-phase framing.
2. **Redaction stage's dispatch policy has no test tying it to reality** (see §4) — the same class of
   silent-drift risk that produced bug #3 above (a config value drifting away from what the code
   actually does, undetected for an unknown period) could recur here too.
3. **Review/edit/approve/export is entirely unbuilt** (Phase 8 remainder) — every `clinical_notes` row
   in the live database is `status='draft'`; nothing has ever been approved or exported by this system.
4. **Reference-label generation via `gpt-oss-120b` is unbuilt** — Phase 5's gold labels are real but
   came from Claude Sonnet 5 directly in-session (decision #72), not the originally-envisioned pipeline.
5. **The eval runner's resumability is partial** — cache-backed for extraction/summary calls (a
   restart doesn't re-spend tokens on those), but the hallucination judge call is not cache-backed, and
   there is no per-item completion checkpoint; a restart re-processes cheaply, not for free.
6. **No prompt-hash propagation into persisted results** — `RunConfig.prompt_set_hash` stays
   hardcoded `""` in `go-api` even though real prompt files exist (decision #70, unchanged this pass) —
   a genuine reproducibility gap distinct from bug #4 above (that one was about the config *itself*
   hashing inconsistently; this one is about a real, computed value never reaching the persisted
   record at all).
7. **`RunConfig.AsrModel` says `"whisper-large-v3-turbo"` while the real backend runs faster-whisper
   `medium`** — already disclosed (decision #58) as a decorative field nothing consumes; left
   unchanged this pass since fixing it doesn't affect behavior (only the honesty of an unread field),
   and touching it would perturb `content_hash` for every future dispatch for no functional gain — but
   it means anyone reading a persisted `run_configs.config` row and taking `asr_model` at face value
   would draw a wrong conclusion about what actually ran.

---

## 6. Recomputed implementation percentage

**39.6%** of v1 (English-only) scope — **unchanged in number from before this pass**, but the meaning
underneath it changed materially, and that needs stating plainly rather than left implicit:

- The percentage methodology (`plan.md`'s own rule: "0% until something runs, 100% only when every
  acceptance criterion can be executed and observed... partial credit only against acceptance criteria
  that individually pass") was already conservative and phase-weighted correctly before this pass. The
  bugs found in §2 do not change *how much of Phase 6/7/10/11 exists* (nothing — still 0%, still the
  overwhelming majority of remaining work), so the phase-weighted arithmetic genuinely still lands at
  39.6%.
- What changed is **confidence in the phases already marked substantially complete**. Before this pass,
  "Phase 3: 100%, done" and "Phase 1: 50%" were true on paper (their named acceptance criteria passed)
  but the system underneath had never actually been proven to reliably finish a real job — five real
  defects were hiding behind fast-fixture-only demonstrations. This pass converts those phases from
  "tested and claimed working" to "tested, claimed working, *and now actually verified working against
  real audio and real timing*" — which is a real increase in the *evidentiary weight* behind the
  existing number, even though the number itself doesn't move.
- **This checkpoint explicitly does not round up to hit 50%.** The honest gap between 39.6% and a
  genuinely-earned 50% is almost entirely Phase 6 (GoT-lite reasoning engine, weight 17%, currently
  0%) — that single phase, if it existed even partially, is the most direct path to 50%, since it
  alone is worth more than the gap. Phases 7/10/11 (0% each) are the rest of the gap. Nothing about
  today's bug-fixing pass substitutes for that missing work, and it would be dishonest to imply
  otherwise by nudging the number.
- v2 (Kannada-English) work is excluded from both numerator and denominator throughout, per the
  existing methodology (`plan.md`'s "v1 phase weights sum to 96... Phase 12 carries no weight").

**If asked "why isn't a system this thoroughly plumbed at 50%": the plumbing (control plane,
orchestration, real ASR, a real single-pass baseline, a real eval harness with real measured numbers,
a working if minimal UI) is real, but it is infrastructure for the ablation, not the ablation itself —
and the ablation (GoT-lite reasoning vs. single-pass baseline, the actual research question) has not
been built or run yet. That is the honest 10.4-point gap.**

---

## 7. Known Gaps and Debt (carried into `claude_context.md`/`plan.md`)

Priority-ordered, not phase-ordered:

1. **Real PII redaction is unbuilt (§5.1).** Highest priority of any gap in this document — a
   compliance-relevant, not merely a completeness, gap. Recommend scoping as its own near-term task
   before Phase 6 work begins, given how directly it bears on the "no data crossing org boundaries" /
   safe-handling spirit of this checkpoint's own milestone definition, even though real redaction was
   not itself named as part of that milestone's literal acceptance list.
2. **GoT-lite reasoning engine (Phase 6) does not exist.** The core research contribution. Everything
   else in this document is infrastructure built to make this phase measurable, not a substitute for
   it.
3. **Knowledge augmentation (Phase 7) does not exist.**
4. **Test coverage gaps that map directly onto today's bugs** (§4): `coda_worker_sdk.consumer`
   (self-reclaim timing), cross-job idempotency-key collision, `_handle_redact` (currently untestable
   as "does it redact" since it doesn't), and the `go/internal/tasks` periodic jobs. Recommend closing
   at least the first two before any further orchestrator-level changes, since both were live,
   silent, production-would-be-affecting bugs.
5. **`RunConfig.prompt_set_hash` and `.AsrModel` don't reflect reality** (§5.6, §5.7) — both disclosed,
   neither fixed, both genuine reproducibility gaps for anyone querying `run_configs` directly.
6. **Review/edit/approve/export (Phase 8 remainder) unbuilt.**
7. **Eval runner resumability is partial** — no per-item checkpoint, hallucination judge calls not
   cached.
8. **v2 extension point, not a v1 debt**: `asr_service/roles.py`'s Doctor/Patient few-shot
   classification prompt is English-only regardless of transcript language — flagged by this pass's
   language-agnosticism audit as a real cost v2 will incur that `claude_context.md` §2.1's extension
   table didn't previously name. No other new language leak was found; everything else audited (proto,
   migrations, prompt directory structure, ASR per-job language config, the frontend's deliberate
   absence of a language selector, eval dataset loaders) was already either correctly language-agnostic
   or an already-documented, intentional v1-only gate.
9. **Clean-clone reproduction (Phase 0's last AC) still unverified** — no README yet documenting host
   tool bootstrap. Unchanged by this pass.

---

## 8. Cheap fixes made during this pass, beyond §2's five bugs

- `frontend/vite.config.ts` had no `test.include` glob, so vitest's default pattern also collected
  Playwright's `e2e/*.spec.ts` — whose `test()` API vitest doesn't understand — crashing `npm run
  test` outright. Fixed by scoping vitest to `src/**` and switching the config's `defineConfig` import
  to `vitest/config` (required for the `test` key's types).
- `docs/architecture.md`'s stale "not built" banner for the Python worker side of §2 and for §6,
  corrected.
