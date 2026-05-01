"""Credential-safe helpers for Git subprocess authentication."""

from __future__ import annotations

import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path


def github_https_url(repo_full_name: str) -> str:
    """Return a GitHub HTTPS URL without embedded credentials."""
    return f"https://github.com/{repo_full_name}.git"


def redact_git_url(url: str) -> str:
    """Redact credentials from a Git URL for logging and diagnostics."""
    if "@" not in url:
        return url
    scheme, _, rest = url.partition("://")
    if not rest or "@" not in rest:
        return url
    return f"{scheme}://<redacted>@{rest.split('@', 1)[1]}"


@dataclass
class GitAuth:
    """Context manager that supplies Git credentials via ``GIT_ASKPASS``.

    The helper script is written to the OS temp directory, not the checkout.
    This keeps tokens out of remote URLs and outside AI-readable worktrees.
    """

    token: str
    username: str = "x-access-token"
    prefix: str = "ci-agent-git-askpass-"
    _askpass_path: str = ""

    def __enter__(self) -> "GitAuth":
        if not self.token:
            return self
        fd, path = tempfile.mkstemp(prefix=self.prefix, suffix=".sh")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(
                "#!/bin/sh\n"
                "case \"$1\" in\n"
                f"  *Username*) echo {self.username} ;;\n"
                "  *) echo \"$GIT_PASSWORD\" ;;\n"
                "esac\n"
            )
        os.chmod(path, stat.S_IRWXU)
        self._askpass_path = path
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.cleanup()

    @property
    def askpass_path(self) -> str:
        return self._askpass_path

    def env(self, base: dict[str, str] | None = None) -> dict[str, str]:
        """Return an environment suitable for authenticated Git commands."""
        env = dict(base or os.environ)
        if self.token:
            env["GIT_TERMINAL_PROMPT"] = "0"
            env["GIT_PASSWORD"] = self.token
            if self._askpass_path:
                env["GIT_ASKPASS"] = self._askpass_path
        return env

    def cleanup(self) -> None:
        if not self._askpass_path:
            return
        try:
            Path(self._askpass_path).unlink()
        except OSError:
            pass
        self._askpass_path = ""
