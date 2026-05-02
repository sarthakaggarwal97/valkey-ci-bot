"""Regression tests for daily.yml validation dispatch inputs."""

from __future__ import annotations

from scripts.ci_validator import _build_skipjobs


def _skip_set(job_name: str) -> set[str]:
    return set(filter(None, _build_skipjobs(job_name).split(",")))


def test_tls_io_threads_job_keeps_tls_and_iothreads() -> None:
    skipjobs = _skip_set("test-ubuntu-tls-io-threads")

    assert "tls" not in skipjobs
    assert "iothreads" not in skipjobs
    assert "valgrind" in skipjobs


def test_generic_io_threads_job_still_skips_tls() -> None:
    skipjobs = _skip_set("test-ubuntu-io-threads")

    assert "iothreads" not in skipjobs
    assert "tls" in skipjobs


def test_unknown_job_runs_all_jobs() -> None:
    assert _build_skipjobs("test-custom-new-daily-job") == ""
