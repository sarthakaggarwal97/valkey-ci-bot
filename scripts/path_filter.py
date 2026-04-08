"""Path filtering for PR review selection."""

from __future__ import annotations

from pathlib import PurePosixPath

from scripts.models import ChangedFile

_GENERATED_SEGMENTS = {
    "dist",
    "vendor",
    "third_party",
    "third-party",
    "node_modules",
    "build",
    "gen",
    "_gen",
    "generated",
    "@generated",
}
_UNSUPPORTED_SUFFIXES = {
    # Images
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".ico", ".svg",
    ".webm", ".woff", ".woff2", ".eot", ".otf", ".ttf",
    # Documents
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    # Archives / binaries
    ".zip", ".gz", ".xz", ".bz2", ".7z", ".rar", ".zst", ".tar",
    ".jar", ".war", ".class", ".dll", ".dylib", ".so", ".exe",
    ".o", ".lo", ".pyc", ".pyd", ".pyo", ".egg",
    ".app", ".bin", ".iso", ".nar", ".wasm",
    # Media
    ".mp3", ".wav", ".wma", ".flac", ".ogg",
    ".mp4", ".avi", ".mkv", ".wmv", ".mov", ".flv",
    ".m4a", ".m4v", ".3gp", ".3g2", ".rm", ".swf",
    # Data / serialized
    ".db", ".csv", ".tsv", ".dat", ".pkl", ".pickle", ".parquet",
    ".pb.go", ".snap", ".tfstate", ".tfstate.backup",
    # Keys / certs
    ".pub", ".pem",
    # Lock / config (rarely need code review)
    ".lock", ".md5sum",
    # Misc
    ".log", ".rkt", ".ss", ".p", ".glif", ".dot",
    ".mmd",
}


def _normalize_path(path: str) -> str:
    return path.lstrip("./")


def _matches(path: str, pattern: str) -> bool:
    return PurePosixPath(_normalize_path(path)).match(pattern)


def _looks_generated(path: str) -> bool:
    normalized = _normalize_path(path)
    if any(segment in _GENERATED_SEGMENTS for segment in normalized.split("/")):
        return True
    lowered = normalized.lower()
    return (
        lowered.endswith(".min.js")
        or lowered.endswith(".min.js.map")
        or lowered.endswith(".min.css")
        or lowered.endswith(".generated.h")
    )


def _unsupported(path: str) -> bool:
    lowered = _normalize_path(path).lower()
    return any(lowered.endswith(suffix) for suffix in _UNSUPPORTED_SUFFIXES)


def is_unsupported_review_path(path: str) -> bool:
    """Return True when a path is not useful for automated code review."""
    return _unsupported(path)


class PathFilter:
    """Applies default exclusions and ordered include/exclude patterns."""

    def select(self, files: list[ChangedFile], patterns: list[str]) -> list[ChangedFile]:
        """Return the subset of changed files eligible for review."""
        return [
            changed_file
            for changed_file in files
            if not self._default_excluded(changed_file)
            and self._allowed_by_patterns(changed_file.path, patterns)
        ]

    def _default_excluded(self, changed_file: ChangedFile) -> bool:
        path = changed_file.path
        return (
            changed_file.is_binary
            or _looks_generated(path)
            or _unsupported(path)
        )

    def _allowed_by_patterns(self, path: str, patterns: list[str]) -> bool:
        if not patterns:
            return True

        allowed = False
        for raw_pattern in patterns:
            if not raw_pattern:
                continue
            exclude = raw_pattern.startswith("!")
            pattern = raw_pattern[1:] if exclude else raw_pattern
            if _matches(path, pattern):
                allowed = not exclude
        return allowed
