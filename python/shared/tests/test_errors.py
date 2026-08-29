from coda.v1 import common_pb2
from coda_worker_sdk.errors import (
    FatalError,
    QuotaExhaustedError,
    RetryableError,
    classify_exception,
)


def test_retryable_error_classified_as_retryable() -> None:
    c = classify_exception(RetryableError("timed out", code="TIMEOUT"))
    assert c.status == common_pb2.Status.STATUS_RETRYABLE
    assert c.error.code == "TIMEOUT"
    assert c.error.retryable is True


def test_fatal_error_classified_as_fatal_not_retryable() -> None:
    c = classify_exception(FatalError("bad input", code="MALFORMED"))
    assert c.status == common_pb2.Status.STATUS_FATAL
    assert c.error.retryable is False


def test_quota_exhausted_carries_resume_after() -> None:
    c = classify_exception(QuotaExhaustedError("cap hit", retry_after_seconds=30.0))
    assert c.status == common_pb2.Status.STATUS_QUOTA_EXHAUSTED
    assert c.resume_after_seconds == 30.0


def test_unclassified_exception_defaults_to_retryable() -> None:
    c = classify_exception(RuntimeError("something broke"))
    assert c.status == common_pb2.Status.STATUS_RETRYABLE
    assert c.error.code == "UNCAUGHT_EXCEPTION"
