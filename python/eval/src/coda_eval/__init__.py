"""coda_eval: the evaluation harness (plan.md Phases 2 and 5).

Reads asr_service's real preprocessing/transcription/diarization to measure
it; never the reverse — a worker service must never import coda_eval
(docs/architecture.md's service-topology prohibitions).
"""
