# PriMock57 ASR/DER baseline

**English-only results.** No multilingual (Kannada-English) evaluation has been performed — v1 is English-only by design (claude_context.md §2.1), and the table below carries a `language` column so a future `kn_en` row is a data addition, not a format change.

These are numbers this project measured itself, on this run, against the public reference data recorded in `data/registry.yaml` — not copied from any prior report or the dataset's own paper.

## Run metadata

- Timestamp (UTC): 2026-08-29T16:44:01.980179+00:00
- Git SHA: `8d3aa9ae42cf37a941c635f306fc3da1e0a42c62`
- ASR backend / model: `faster_whisper_local` / `medium/int8`
- Diarization model: `not run this session — see caveats`

## Caveats

- HF_TOKEN was not set — pyannote.audio diarization did not run, so DER is not reported this run. WER/CER do not depend on HF_TOKEN and are real measured numbers.

## Results by dataset and language subset

| Dataset | Language | N | WER | CER | DER | DER: miss | DER: false alarm | DER: confusion | Notes |
|---|---|---|---|---|---|---|---|---|---|
| primock57 | en | 8 | 0.2226 | 0.1666 |  |  |  |  | 8 of 57 upstream consultations (subset actually fetched) |
