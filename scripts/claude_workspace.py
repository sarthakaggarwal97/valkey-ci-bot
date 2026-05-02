from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Generator

from scripts.git_auth import GitAuth, github_https_url

logger = logging.getLogger(__name__)


@dataclass
class WorkspaceContext:
    tmpdir: str
    git_env: dict[str, str]
    repo: str
    ref: str | None

    def capture_diff(self, *, binary: bool = True) -> str:
        """Capture tracked + newly-created files as unified patch."""
        subprocess.run(['git', 'add', '-N', '.'], cwd=self.tmpdir, capture_output=True, text=True)
        args = ['git', 'diff']
        if binary:
            args.append('--binary')
        result = subprocess.run(args, cwd=self.tmpdir, capture_output=True, text=True)
        return result.stdout.strip()


@contextmanager
def claude_workspace(
    repo: str,
    ref: str | None,
    token: str | None = None,
    *,
    prefix: str = 'claude-ws-',
    clone_depth: int | None = None,
    extra_fetch_refs: list[str] | None = None,
) -> Generator[WorkspaceContext, None, None]:
    """Clone repo to a temp dir, optionally checkout a ref, yield WorkspaceContext.

    Handles GitAuth askpass setup + cleanup automatically.
    """
    tmpdir = tempfile.mkdtemp(prefix=prefix)
    git_auth: GitAuth | None = None
    try:
        git_env = os.environ.copy()
        if token:
            git_auth = GitAuth(token, prefix=f'{prefix}askpass-')
            git_auth.__enter__()
            git_env = git_auth.env()

        clone_url = github_https_url(repo)
        clone_args = ['git', 'clone', '--filter=blob:none']
        if clone_depth is not None:
            clone_args.extend(['--depth', str(clone_depth)])
        clone_args.extend([clone_url, tmpdir])
        subprocess.run(
            clone_args,
            env=git_env,
            capture_output=True,
            text=True,
            check=True,
            timeout=180,
        )

        if ref is not None:
            # Fetch the ref in case it's a PR head or non-default branch
            subprocess.run(
                ['git', 'fetch', '--depth', '50', 'origin', ref],
                cwd=tmpdir,
                env=git_env,
                capture_output=True,
                text=True,
                timeout=90,
            )
            subprocess.run(
                ['git', 'checkout', '--detach', 'FETCH_HEAD'],
                cwd=tmpdir,
                env=git_env,
                capture_output=True,
                text=True,
                check=True,
                timeout=60,
            )

        for extra_ref in extra_fetch_refs or []:
            subprocess.run(
                ['git', 'fetch', 'origin', extra_ref],
                cwd=tmpdir,
                env=git_env,
                capture_output=True,
                text=True,
                timeout=90,
            )

        yield WorkspaceContext(tmpdir=tmpdir, git_env=git_env, repo=repo, ref=ref)
    finally:
        if git_auth is not None:
            git_auth.cleanup()
        shutil.rmtree(tmpdir, ignore_errors=True)
