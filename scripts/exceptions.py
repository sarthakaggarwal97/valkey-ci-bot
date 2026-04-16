"""Custom exception types for the CI agent.

Using specific exception types instead of bare ``Exception`` makes error
handling more precise and debugging easier.
"""

from __future__ import annotations


class CIAgentError(Exception):
    """Base exception for all CI agent errors."""


class GitHubAPIError(CIAgentError):
    """Error communicating with the GitHub API."""


class ConfigurationError(CIAgentError):
    """Invalid or missing configuration."""


class StoreError(CIAgentError):
    """Error reading or writing persistent state."""


class StoreConflictError(StoreError):
    """Optimistic concurrency conflict on state write."""


class ParseError(CIAgentError):
    """Error parsing log content or structured data."""


class ValidationError(CIAgentError):
    """Fix validation failed."""


class RateLimitExceeded(CIAgentError):
    """Rate limit or budget exceeded."""


class AnalysisError(CIAgentError):
    """Error during root cause analysis or fix generation."""
