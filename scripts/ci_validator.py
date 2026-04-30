"""CI validator — dispatches daily.yml on the fork to validate a fix.

Pushes the fix to a branch, dispatches the daily workflow with skipjobs
set to run only the failing job, and polls until the run completes.
"""

from __future__ import annotations

import json
import logging
import time
from urllib import error as urllib_error
from urllib import request as urllib_request

logger = logging.getLogger(__name__)

# All skipjobs keywords used in daily.yml. To run ONLY one job, we set
# skipjobs to every keyword EXCEPT the one(s) the target job needs.
_ALL_SKIP_KEYWORDS = [
    "valgrind", "sanitizer", "tls", "freebsd", "macos", "alpine",
    "32bit", "iothreads", "ubuntu", "rpm-distros", "malloc",
    "specific", "fortify", "reply-schema", "arm", "lttng",
]

# Map from job-name substrings to the skipjobs keyword(s) that must be
# ABSENT for that job to run. Order matters: first match wins.
_JOB_TO_KEEP_KEYWORDS: list[tuple[str, list[str]]] = [
    ("valgrind", ["valgrind"]),
    ("sanitizer", ["sanitizer"]),
    ("fortify", ["fortify"]),
    ("32bit", ["32bit"]),
    ("io-threads", ["iothreads"]),
    ("tls-io-threads", ["tls", "iothreads"]),
    ("tls-module-no-tls", ["tls", "rpm-distros"]),
    ("tls-module", ["tls", "rpm-distros"]),
    ("tls-no-tls", ["tls"]),
    ("tls", ["tls"]),
    ("rpm-distros", ["rpm-distros"]),
    ("alpine", ["alpine"]),
    ("freebsd", ["freebsd"]),
    ("macos", ["macos"]),
    ("arm", ["arm", "ubuntu"]),
    ("lttng", ["lttng"]),
    ("reply-schema", ["reply-schema"]),
    ("malloc", ["malloc"]),
    ("ubuntu", ["ubuntu"]),
]


def _build_skipjobs(job_name: str) -> str:
    """Build a skipjobs string that runs ONLY the given job.

    Returns a comma-separated string of keywords to skip. The target
    job's keyword(s) are excluded so its ``if:`` condition passes.
    """
    keep: set[str] = set()
    job_lower = job_name.lower()
    for pattern, keywords in _JOB_TO_KEEP_KEYWORDS:
        if pattern in job_lower:
            keep.update(keywords)
            break
    if not keep:
        # Unknown job — don't skip anything, let all jobs run.
        logger.warning(
            "No skipjobs mapping for job '%s'; running all jobs.", job_name,
        )
        return ""
    return ",".join(kw for kw in _ALL_SKIP_KEYWORDS if kw not in keep)


def dispatch_validation(
    *,
    token: str,
    fork_repo: str,
    fix_branch: str,
    job_name: str,
    test_file: str,
    loop_count: int = 100,
    extra_test_args: str = "",
) -> int | None:
    """Dispatch daily.yml on the fork and return the workflow run ID.

    Returns None if the dispatch fails.
    """
    skipjobs = _build_skipjobs(job_name)
    test_args = f"--single {test_file}" if test_file else ""
    if loop_count > 1:
        test_args += f" --loop {loop_count}"
    if extra_test_args:
        test_args += f" {extra_test_args}"

    inputs = {
        "use_repo": fork_repo,
        "use_git_ref": fix_branch,
        "skipjobs": skipjobs,
        "test_args": test_args.strip(),
    }

    url = (
        f"https://api.github.com/repos/{fork_repo}/actions/workflows/"
        "daily.yml/dispatches"
    )
    payload = json.dumps({"ref": "unstable", "inputs": inputs}).encode()
    req = urllib_request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "valkey-ci-agent",
        },
    )
    try:
        with urllib_request.urlopen(req, timeout=30) as resp:
            logger.info(
                "Dispatched daily.yml on %s (branch=%s, skipjobs=%s, test_args=%s). "
                "HTTP %d.",
                fork_repo, fix_branch, skipjobs[:60], test_args[:60], resp.status,
            )
    except urllib_error.HTTPError as exc:
        logger.error(
            "Dispatch failed: HTTP %d: %s",
            exc.code, exc.read().decode("utf-8", errors="replace")[:500],
        )
        return None
    except Exception as exc:
        logger.error("Dispatch failed: %s", exc)
        return None

    # GitHub doesn't return the run ID from dispatch. We need to find it
    # by listing recent runs on the workflow filtered by branch.
    time.sleep(5)  # Give GitHub a moment to create the run.
    return _find_dispatched_run(token, fork_repo, fix_branch)


def _find_dispatched_run(
    token: str, repo: str, branch: str, max_attempts: int = 10,
) -> int | None:
    """Poll for the most recent workflow run on the given branch."""
    url = (
        f"https://api.github.com/repos/{repo}/actions/workflows/daily.yml/runs"
        f"?branch={branch}&event=workflow_dispatch&per_page=1"
    )
    for attempt in range(max_attempts):
        try:
            req = urllib_request.Request(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "valkey-ci-agent",
                },
            )
            with urllib_request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
                runs = data.get("workflow_runs", [])
                if runs:
                    run = runs[0]
                    run_id = run["id"]
                    logger.info(
                        "Found dispatched run %d (status=%s) on %s/%s.",
                        run_id, run.get("status"), repo, branch,
                    )
                    return run_id
        except Exception as exc:
            logger.warning("Run lookup attempt %d failed: %s", attempt + 1, exc)
        time.sleep(10)
    logger.error("Could not find dispatched run on %s/%s after %d attempts.", repo, branch, max_attempts)
    return None


def poll_run(
    token: str,
    repo: str,
    run_id: int,
    *,
    poll_interval: int = 60,
    timeout: int = 5400,  # 90 minutes
) -> tuple[bool, str, str]:
    """Poll a workflow run until it completes.

    Returns ``(passed, conclusion, run_url)``.
    """
    url = f"https://api.github.com/repos/{repo}/actions/runs/{run_id}"
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        try:
            req = urllib_request.Request(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "valkey-ci-agent",
                },
            )
            with urllib_request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
                status = data.get("status", "")
                conclusion = data.get("conclusion", "")
                run_url = data.get("html_url", "")
                if status == "completed":
                    passed = conclusion == "success"
                    logger.info(
                        "Run %d completed: conclusion=%s, passed=%s. %s",
                        run_id, conclusion, passed, run_url,
                    )
                    return passed, conclusion, run_url
                logger.debug(
                    "Run %d still %s (elapsed %.0fs).",
                    run_id, status, time.monotonic() - start,
                )
        except Exception as exc:
            logger.warning("Poll error for run %d: %s", run_id, exc)
        time.sleep(poll_interval)

    logger.error("Run %d timed out after %ds.", run_id, timeout)
    return False, "timeout", f"https://github.com/{repo}/actions/runs/{run_id}"
