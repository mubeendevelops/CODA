"""coda-eval CLI. Subcommands:

  coda-eval registry          — (re)build the item manifest + data/registry.yaml
                                 from whatever's present under data/raw/
  coda-eval splits            — assign + validate train/dev/test splits (per language)
  coda-eval run-primock57-asr — the ASR/DER measurement run (plan.md Phase 2):
                                 preprocess -> faster-whisper transcribe -> (if HF_TOKEN
                                 is set) pyannote diarize -> WER/CER/DER against
                                 PriMock57's own reference transcripts/RTTM, written to
                                 eval_runs/eval_results and docs/eval/*.md + *.csv
"""

from __future__ import annotations

import logging
import os
import sys

import click
from faster_whisper import WhisperModel  # type: ignore[import-untyped]
from pyannote.core import Annotation, Segment  # type: ignore[import-untyped]

from asr_service.audio import preprocess
from asr_service.diarize import diarize as run_diarization
from asr_service.diarize import load_pipeline
from asr_service.transcribe import transcribe
from coda_eval import acibench, db, mtsdialog, primock57
from coda_eval.config import (
    DATA_REGISTRY_DIR,
    DATA_SPLITS_DIR,
    DATASET_REGISTRY_PATH,
    DOCS_EVAL_DIR,
    REPO_ROOT,
    PostgresConfig,
)
from coda_eval.metrics.der import DerAccumulator
from coda_eval.metrics.wer_cer import aggregate_wer_cer, compute_wer_cer
from coda_eval.registry import DatasetSummary, write_dataset_registry, write_manifest
from coda_eval.report import RunMetadata, SubsetResult, now_utc, write_csv, write_markdown
from coda_eval.rttm import read_rttm, rttm_to_annotation
from coda_eval.splits import assert_no_leakage, assign_splits

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("coda_eval")


@click.group()
def main() -> None:
    pass


@main.command()
def registry() -> None:
    """Builds the item manifest for every loader and the dataset-level
    data/registry.yaml (licence per dataset — Phase 2 acceptance criterion 1).
    """
    pm_entries = primock57.build_manifest()
    mts_entries = mtsdialog.build_manifest()
    aci_entries = acibench.build_manifest()

    write_manifest(pm_entries, DATA_REGISTRY_DIR / f"{primock57.DATASET_NAME}.manifest.jsonl")
    write_manifest(mts_entries, DATA_REGISTRY_DIR / f"{mtsdialog.DATASET_NAME}.manifest.jsonl")
    write_manifest(aci_entries, DATA_REGISTRY_DIR / f"{acibench.DATASET_NAME}.manifest.jsonl")

    summaries = [
        DatasetSummary(
            name=primock57.DATASET_NAME,
            source_url=primock57.SOURCE_URL,
            licence=primock57.LICENCE,
            modality="audio_grounded",
            languages=["en"],
            intended_use="dev+eval",
            n_items_available_locally=len(pm_entries),
            n_items_upstream=primock57.N_ITEMS_UPSTREAM,
            manifest_path=str(DATA_REGISTRY_DIR / f"{primock57.DATASET_NAME}.manifest.jsonl"),
        ),
        DatasetSummary(
            name=mtsdialog.DATASET_NAME,
            source_url=mtsdialog.SOURCE_URL,
            licence=mtsdialog.LICENCE,
            modality="text_only",
            languages=["en"],
            intended_use="dev",
            n_items_available_locally=len(mts_entries),
            n_items_upstream=len(mts_entries),
            manifest_path=str(DATA_REGISTRY_DIR / f"{mtsdialog.DATASET_NAME}.manifest.jsonl"),
        ),
        DatasetSummary(
            name=acibench.DATASET_NAME,
            source_url=acibench.SOURCE_URL,
            licence=acibench.LICENCE,
            modality="text_only",
            languages=["en"],
            intended_use="dev",
            n_items_available_locally=len(aci_entries),
            n_items_upstream=len(aci_entries),
            manifest_path=str(DATA_REGISTRY_DIR / f"{acibench.DATASET_NAME}.manifest.jsonl"),
        ),
    ]
    write_dataset_registry(summaries, DATASET_REGISTRY_PATH)
    for s in summaries:
        logger.info(
            "registry: %s — %d items locally (%s)", s.name, s.n_items_available_locally, s.licence
        )


@main.command()
@click.option("--dataset", required=True)
def splits(dataset: str) -> None:
    """Assigns train/dev/test splits by session ID (never utterance) and
    asserts no leakage, writing the split-assigned manifest to data/splits/.
    """
    from coda_eval.registry import read_manifest

    manifest_path = DATA_REGISTRY_DIR / f"{dataset}.manifest.jsonl"
    entries = read_manifest(manifest_path)
    split_entries = assign_splits(entries)
    assert_no_leakage(split_entries)
    out_path = DATA_SPLITS_DIR / f"{dataset}.manifest.jsonl"
    write_manifest(split_entries, out_path)
    counts: dict[str, int] = {}
    for e in split_entries:
        counts[e.split or "?"] = counts.get(e.split or "?", 0) + 1
    logger.info("splits: %s -> %s (%d entries, no leakage)", dataset, counts, len(split_entries))


def _reference_words_only(transcript_path: str) -> str:
    """PriMock57's collated reference is "Speaker: text" per line; strip the
    speaker label so WER compares words against words, not against a label
    our own hypothesis text never contains either.
    """
    lines = []
    with open(transcript_path, encoding="utf-8") as f:
        for line in f:
            _, _, text = line.partition(": ")
            lines.append(text.strip())
    return " ".join(lines)


@main.command(name="run-primock57-asr")
@click.option("--limit", type=int, default=None, help="Only process the first N items.")
@click.option("--model-size", default=os.environ.get("ASR_MODEL_SIZE", "medium"))
@click.option("--compute-type", default=os.environ.get("ASR_COMPUTE_TYPE", "int8"))
@click.option("--device", default=os.environ.get("ASR_DEVICE", "cpu"))
@click.option("--language", default="en")
@click.option("--no-db", is_flag=True, help="Skip writing to Postgres (report files only).")
def run_primock57_asr(
    limit: int | None, model_size: str, compute_type: str, device: str, language: str, no_db: bool
) -> None:
    """The ASR/DER measurement run against real PriMock57 audio present on
    disk. WER/CER always run (faster-whisper needs no credential). DER runs
    only if HF_TOKEN is set — otherwise it's skipped with an honest caveat
    in the report rather than faked or silently omitted.
    """
    from coda_eval.registry import read_manifest

    split_manifest_path = DATA_SPLITS_DIR / f"{primock57.DATASET_NAME}.manifest.jsonl"
    if not split_manifest_path.exists():
        logger.error("no split manifest at %s — run `coda-eval registry` then `coda-eval splits "
                      "--dataset primock57` first", split_manifest_path)
        sys.exit(1)

    entries = [e for e in read_manifest(split_manifest_path) if e.language == language]
    if limit is not None:
        entries = entries[:limit]
    if not entries:
        logger.error("no manifest entries for language=%s", language)
        sys.exit(1)

    logger.info("loading faster-whisper %s/%s/%s", model_size, compute_type, device)
    whisper_model = WhisperModel(model_size, device=device, compute_type=compute_type)

    hf_token = os.environ.get("HF_TOKEN", "")
    diarization_model_id = "pyannote/speaker-diarization-3.1"
    diarizer = None
    caveats: list[str] = []
    if hf_token:
        try:
            logger.info("loading pyannote diarization pipeline")
            diarizer = load_pipeline(diarization_model_id, hf_token, device=device)
        except Exception as exc:
            logger.warning("diarization pipeline failed to load, DER will be skipped: %s", exc)
            caveats.append(f"Diarization pipeline failed to load ({exc}); DER not computed.")
    else:
        caveats.append(
            "HF_TOKEN was not set — pyannote.audio diarization did not run, so DER is not "
            "reported this run. WER/CER do not depend on HF_TOKEN and are real measured numbers."
        )

    pg_conn = None
    run_config_id = eval_run_id = None
    if not no_db:
        try:
            pg_conn = db.connect(PostgresConfig.from_env())
        except Exception as exc:
            logger.warning("could not connect to Postgres, results will not be persisted: %s", exc)
            caveats.append(
                f"Postgres was unreachable ({exc}); results were not written to "
                "eval_runs/eval_results."
            )

    if pg_conn is not None:
        sha = db.git_sha(REPO_ROOT)
        config = {
            "eval_kind": "asr_der",
            "dataset": primock57.DATASET_NAME,
            "asr_backend": "faster_whisper_local",
            "asr_model": f"{model_size}/{compute_type}",
            "diarization_model": diarization_model_id if diarizer else None,
            "git_sha": sha,
            "language": language,
        }
        run_config_id = db.intern_run_config(
            pg_conn, config=config, arm="baseline", schema_version=1
        )
        eval_run_id = db.create_eval_run(
            pg_conn,
            name=f"primock57-asr-der-{now_utc().date().isoformat()}",
            dataset_split="mixed",
            arm="baseline",
            run_config_id=run_config_id,
        )
        pg_conn.commit()

    wer_cer_results = []
    der_accumulator = DerAccumulator() if diarizer is not None else None
    n_der_items = 0

    for entry in entries:
        logger.info("processing %s", entry.item_id)
        with open(entry.audio_path, "rb") as f:  # type: ignore[arg-type]
            raw = f.read()
        audio = preprocess(
            raw, ext="wav", target_loudness_dbfs=-20.0, min_duration_s=0.5, max_duration_s=4 * 3600
        )

        transcription = transcribe(
            whisper_model,
            audio.samples,
            sample_rate=audio.sample_rate,
            language=language,
            chunk_length_s=300,
            chunk_overlap_s=5,
        )
        hypothesis_text = "".join(w.text for w in transcription.words).strip()
        reference_text = _reference_words_only(entry.reference_transcript_path)  # type: ignore[arg-type]

        result = compute_wer_cer(reference_text, hypothesis_text)
        wer_cer_results.append(result)
        logger.info(
            "  WER=%.4f CER=%.4f (%d ref words)",
            result.wer, result.cer, result.reference_word_count,
        )

        der_value = None
        if diarizer is not None and der_accumulator is not None:
            segments = run_diarization(diarizer, audio.samples, audio.sample_rate)
            hyp_annotation = Annotation(uri=entry.session_id)
            for seg in segments:
                hyp_annotation[Segment(seg.start_s, seg.end_s)] = seg.speaker
            ref_segments = read_rttm(entry.reference_rttm_path)  # type: ignore[arg-type]
            ref_annotation = rttm_to_annotation(ref_segments, uri=entry.session_id)
            der_result = der_accumulator.add(ref_annotation, hyp_annotation)
            der_value = der_result.der
            n_der_items += 1
            logger.info("  DER=%.4f (miss=%.4f fa=%.4f conf=%.4f)", der_result.der,
                        der_result.missed_detection, der_result.false_alarm, der_result.confusion)

        if pg_conn is not None and run_config_id and eval_run_id:
            ref = db.ensure_dataset_item_consultation(
                pg_conn, item_id=entry.item_id, language=language, licence=entry.licence
            )
            for metric_key, metric_value in (("wer", result.wer), ("cer", result.cer)):
                db.write_eval_result(
                    pg_conn, eval_run_id=eval_run_id, consultation_id=ref.consultation_id,
                    run_config_id=run_config_id, metric_key=metric_key, metric_value=metric_value,
                    language=language, reference_provenance="dataset_gold",
                )
            if der_value is not None:
                db.write_eval_result(
                    pg_conn, eval_run_id=eval_run_id, consultation_id=ref.consultation_id,
                    run_config_id=run_config_id, metric_key="der", metric_value=der_value,
                    language=language, reference_provenance="dataset_gold",
                )
            pg_conn.commit()

    aggregate = aggregate_wer_cer(wer_cer_results)
    der_agg = der_accumulator.result() if der_accumulator is not None and n_der_items else None

    if pg_conn is not None and eval_run_id:
        db.finish_eval_run(pg_conn, eval_run_id=eval_run_id, status="completed")
        pg_conn.commit()
        pg_conn.close()

    subset = SubsetResult(
        dataset=primock57.DATASET_NAME,
        language=language,
        n_items=len(entries),
        wer=aggregate.wer,
        cer=aggregate.cer,
        der=der_agg.der if der_agg else None,
        der_missed_detection=der_agg.missed_detection if der_agg else None,
        der_false_alarm=der_agg.false_alarm if der_agg else None,
        der_confusion=der_agg.confusion if der_agg else None,
        notes=(
            f"{len(entries)} of {primock57.N_ITEMS_UPSTREAM} upstream consultations "
            "(subset actually fetched)"
        ),
    )
    meta = RunMetadata(
        run_name="PriMock57 ASR/DER baseline",
        git_sha=db.git_sha(REPO_ROOT),
        asr_backend="faster_whisper_local",
        asr_model=f"{model_size}/{compute_type}",
        diarization_model=diarization_model_id if diarizer else None,
        timestamp=now_utc(),
        honest_caveats=caveats,
    )
    write_markdown([subset], meta, DOCS_EVAL_DIR / "primock57_asr_der_baseline.md")
    write_csv([subset], DOCS_EVAL_DIR / "primock57_asr_der_baseline.csv")
    logger.info(
        "done: WER=%.4f CER=%.4f DER=%s over %d items — report written to docs/eval/",
        aggregate.wer, aggregate.cer, f"{der_agg.der:.4f}" if der_agg else "not computed",
        len(entries),
    )


if __name__ == "__main__":
    main()
