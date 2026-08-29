# ADR-0014 — DPDP posture, and PII redaction placed between ASR and NLP

- **Status:** Accepted
- **Date:** 2026-08-27
- **Related:** ADR-0006, ADR-0008, decision #1

## Context

India's Digital Personal Data Protection Act, 2023 governs health data in this jurisdiction — not
HIPAA, which is the common US-centric assumption. Relevant obligations: lawful consent, purpose
limitation, data minimisation, reasonable security safeguards, and erasure on withdrawal.

The pipeline sends transcript text to Groq, a US-based third-party processor. Some point in the
pipeline must be the boundary beyond which identifying information does not travel.

## Decision

**Redaction is a mandatory, unskippable pipeline state (`REDACT_RUNNING`) between ASR and NLP** — the
last point before transcript text reaches a third-party LLM. It cannot be bypassed, because the
orchestrator owns transitions exclusively (ADR-0006).

Mechanism: pattern and NER-based detection of names, ages, phone numbers, addresses, and identifiers in
`turns.text`, producing `turns.text_redacted` with stable placeholders (`[PATIENT_NAME]`, `[PHONE_1]`).
The reversible mapping is stored encrypted so the review UI can show the doctor original text, while
**only redacted text is ever placed in a prompt**.

Supporting controls: consent as a `NOT NULL` foreign key so an unconsented consultation is not
representable; consent re-checked at every stage dispatch so mid-pipeline revocation halts processing;
append-only `audit_log` written in the same transaction as the audited action; approval required before
export, enforced at schema, state machine, and API layers simultaneously.

## Consequences

**Positive.** The redaction boundary is structural rather than conventional — no worker bug can route
unredacted text to a provider. Consent is a schema invariant, not an application check. Erasure on
withdrawal is implementable because artifact keys are enumerable by consultation prefix (ADR-0005).

**Negative — stated explicitly, not hidden.** With `asr_backend = groq`, **the raw audio has already
crossed to a US-based processor before redaction can act.** The redaction boundary is fully effective
only under `asr_backend = faster_whisper_local`. This is precisely why local ASR is a first-class
swappable backend rather than an afterthought, and why `RunConfig` records which backend produced every
transcript.

For this project the exposure is acceptable: the system processes **role-play and public-dataset audio
only, with no real patient data** (decision #1; `plan.md` Phase 12 — v2, formerly Phase 9 — is
explicitly simulated). For real patient data
the local backend would be mandatory, and the report says so rather than implying the hosted path is
compliant.

Further disclosed gaps: dev Compose runs plaintext on the internal Docker network (TLS is a production
configuration); encryption keys come from environment secrets rather than a KMS, though the key-access
interface is written so that substitution is a config change.

## Alternatives considered

- **Redact at export only.** Rejected: identifying information would reach the provider in every prompt
  — the exact exposure the boundary exists to prevent.
- **Redact before ASR.** Not possible: identifiers are unknown until speech is transcribed.
- **Local ASR mandatory for all runs.** Rejected as default on throughput (20 min/consultation on
  laptop CPU vs ~2 min hosted) which would make the eval sweep impractical. Retained as a configurable
  backend and documented as required for any real-data deployment.
- **Claim HIPAA compliance.** Rejected as simply the wrong jurisdiction.
