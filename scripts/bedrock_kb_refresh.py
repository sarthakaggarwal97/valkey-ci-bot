"""Refresh the Valkey retrieval knowledge bases used by the bot."""

from __future__ import annotations

import argparse
import json
import logging
import mimetypes
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

DEFAULT_REPO_URL = "https://github.com/valkey-io/valkey.git"
DEFAULT_BRANCH = "unstable"
DEFAULT_CODE_KB_ID = "OHQMPN9RCG"
DEFAULT_CODE_DATA_SOURCE_NAME = "valkey-code-custom"
DEFAULT_WEB_KB_ID = "NAKLE24DH9"
DEFAULT_WEB_DATA_SOURCE_NAME = "valkey-docs-web"
DEFAULT_WEB_URLS = [
    "https://valkey.io/",
    "https://valkey.io/topics/",
    "https://valkey.io/commands/",
]

MAX_TEXT_FILE_BYTES = 1_000_000
FIXED_CHUNK_TOKENS = 300
FIXED_CHUNK_OVERLAP = 20
CUSTOM_BATCH_DOC_LIMIT = 25
CUSTOM_BATCH_BYTE_LIMIT = 5_500_000
DATA_SOURCE_POLL_INTERVAL_SECONDS = 5
DATA_SOURCE_POLL_TIMEOUT_SECONDS = 300
DOCUMENT_OPERATION_RETRY_LIMIT = 40
DOCUMENT_OPERATION_RETRY_BASE_SECONDS = 15
TERMINAL_DOCUMENT_STATUSES = {
    "FAILED",
    "IGNORED",
    "INDEXED",
    "METADATA_PARTIALLY_INDEXED",
    "METADATA_UPDATE_FAILED",
    "NOT_FOUND",
    "PARTIALLY_INDEXED",
}
LANGUAGE_BY_SUFFIX = {
    ".c": "c",
    ".cc": "cpp",
    ".cmake": "cmake",
    ".cpp": "cpp",
    ".css": "css",
    ".go": "go",
    ".h": "c",
    ".hpp": "cpp",
    ".html": "html",
    ".java": "java",
    ".js": "javascript",
    ".json": "json",
    ".lua": "lua",
    ".md": "markdown",
    ".pl": "perl",
    ".py": "python",
    ".rb": "ruby",
    ".rst": "rst",
    ".sh": "bash",
    ".sql": "sql",
    ".tcl": "tcl",
    ".toml": "toml",
    ".ts": "typescript",
    ".txt": "text",
    ".xml": "xml",
    ".yaml": "yaml",
    ".yml": "yaml",
}
TEXT_SUFFIXES = {
    ".ac",
    ".c",
    ".cc",
    ".cfg",
    ".cmake",
    ".conf",
    ".cpp",
    ".css",
    ".go",
    ".h",
    ".hpp",
    ".html",
    ".ini",
    ".java",
    ".js",
    ".json",
    ".lua",
    ".m4",
    ".md",
    ".mk",
    ".pl",
    ".py",
    ".rb",
    ".rst",
    ".sh",
    ".sql",
    ".tcl",
    ".text",
    ".toml",
    ".ts",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
TEXT_FILENAMES = {
    "CMakeLists.txt",
    "LICENSE",
    "Makefile",
    "README",
    "README.md",
}
SKIP_DIR_NAMES = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    "node_modules",
}
SKIP_SUFFIXES = {
    ".a",
    ".bin",
    ".bmp",
    ".class",
    ".dll",
    ".dylib",
    ".gif",
    ".gz",
    ".ico",
    ".jar",
    ".jpeg",
    ".jpg",
    ".o",
    ".obj",
    ".pdf",
    ".png",
    ".pyc",
    ".so",
    ".tar",
    ".tgz",
    ".tiff",
    ".wav",
    ".webp",
    ".xz",
    ".zip",
}


@dataclass(frozen=True)
class RefreshArgs:
    """Arguments required to refresh the retrieval knowledge bases."""

    region: str
    repo_url: str
    branch: str
    code_kb_id: str
    code_data_source_name: str
    web_kb_id: str
    web_data_source_id: str | None
    web_data_source_name: str
    web_seed_urls: list[str]
    skip_web_sync: bool
    missing_only: bool
    dry_run: bool
    verbose: bool


@dataclass(frozen=True)
class CuratedDocument:
    """A source file transformed into a Bedrock custom document."""

    document_id: str
    relative_path: str
    text_bytes: int
    payload: dict[str, object]


def build_parser() -> argparse.ArgumentParser:
    """Create CLI parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--region",
        default=os.environ.get("AWS_REGION") or boto3.Session().region_name or "us-east-1",
    )
    parser.add_argument("--repo-url", default=DEFAULT_REPO_URL)
    parser.add_argument("--branch", default=DEFAULT_BRANCH)
    parser.add_argument("--code-kb-id", default=DEFAULT_CODE_KB_ID)
    parser.add_argument("--code-data-source-name", default=DEFAULT_CODE_DATA_SOURCE_NAME)
    parser.add_argument("--web-kb-id", default=DEFAULT_WEB_KB_ID)
    parser.add_argument("--web-data-source-id")
    parser.add_argument("--web-data-source-name", default=DEFAULT_WEB_DATA_SOURCE_NAME)
    parser.add_argument(
        "--web-seed-url",
        action="append",
        dest="web_seed_urls",
        help="Repeatable. If omitted, curated Valkey doc URLs are used.",
    )
    parser.add_argument("--skip-web-sync", action="store_true")
    parser.add_argument(
        "--missing-only",
        action="store_true",
        help="Only ingest files not already present in the code KB.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Prepare the corpus and resolve data sources without mutating Bedrock.",
    )
    parser.add_argument("--verbose", action="store_true")
    return parser


def configure_logging(verbose: bool) -> None:
    """Configure log output."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def parse_args(argv: list[str] | None = None) -> RefreshArgs:
    """Parse command-line arguments."""
    ns = build_parser().parse_args(argv)
    return RefreshArgs(
        region=ns.region,
        repo_url=ns.repo_url,
        branch=ns.branch,
        code_kb_id=ns.code_kb_id,
        code_data_source_name=ns.code_data_source_name,
        web_kb_id=ns.web_kb_id,
        web_data_source_id=ns.web_data_source_id,
        web_data_source_name=ns.web_data_source_name,
        web_seed_urls=ns.web_seed_urls or list(DEFAULT_WEB_URLS),
        skip_web_sync=ns.skip_web_sync,
        missing_only=ns.missing_only,
        dry_run=ns.dry_run,
        verbose=ns.verbose,
    )


def run_command(args: list[str], cwd: Path | None = None) -> str:
    """Run a subprocess and return stdout."""
    logger.debug("Running command: %s", " ".join(args))
    completed = subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        check=True,
        text=True,
        capture_output=True,
    )
    return completed.stdout.strip()


def make_client_token(prefix: str) -> str:
    """Generate a Bedrock-compatible idempotency token."""
    return f"{prefix}-{int(datetime.now(timezone.utc).timestamp())}-{uuid.uuid4().hex}"


def is_probably_text(path: Path) -> bool:
    """Heuristic to skip obviously binary files."""
    guessed, _ = mimetypes.guess_type(path.name)
    if guessed and not guessed.startswith("text/"):
        return False

    with path.open("rb") as handle:
        sample = handle.read(4096)
    return b"\0" not in sample


def should_include_file(path: Path, root: Path) -> bool:
    """Return whether a file should be included in the curated corpus."""
    rel = path.relative_to(root)
    if any(part in SKIP_DIR_NAMES for part in rel.parts):
        return False
    if path.stat().st_size > MAX_TEXT_FILE_BYTES:
        return False
    if path.suffix.lower() in SKIP_SUFFIXES:
        return False
    if path.name in TEXT_FILENAMES or path.suffix.lower() in TEXT_SUFFIXES:
        return is_probably_text(path)
    return False


def clone_repo(repo_url: str, branch: str, destination: Path) -> str:
    """Clone the repository and return the resolved head SHA."""
    run_command(
        ["git", "clone", "--depth", "1", "--branch", branch, repo_url, str(destination)],
    )
    return run_command(["git", "rev-parse", "HEAD"], cwd=destination)


def stage_curated_corpus(
    source_root: Path,
    staging_root: Path,
    repo_url: str,
    branch: str,
    head_sha: str,
) -> tuple[int, int]:
    """Copy curated files into a staging directory and write a manifest."""
    file_count = 0
    total_bytes = 0
    for path in source_root.rglob("*"):
        if not path.is_file() or not should_include_file(path, source_root):
            continue

        rel = path.relative_to(source_root)
        dest = staging_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)
        file_count += 1
        total_bytes += path.stat().st_size

    manifest = {
        "source": repo_url,
        "branch": branch,
        "head_sha": head_sha,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "file_count": file_count,
        "total_bytes": total_bytes,
    }
    manifest_path = staging_root / "_metadata" / "manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    return file_count, total_bytes


def infer_repo_slug(repo_url: str) -> str:
    """Derive owner/repo from a GitHub clone URL."""
    trimmed = repo_url[:-4] if repo_url.endswith(".git") else repo_url
    if trimmed.startswith("https://github.com/"):
        return trimmed.removeprefix("https://github.com/")
    if trimmed.startswith("git@github.com:"):
        return trimmed.removeprefix("git@github.com:")
    return trimmed


def infer_language(path: Path) -> str | None:
    """Map a file extension to a display language when possible."""
    return LANGUAGE_BY_SUFFIX.get(path.suffix.lower())


def make_document_text(
    relative_path: str,
    source_text: str,
    repo_slug: str,
    branch: str,
    head_sha: str,
) -> str:
    """Wrap file contents with repository metadata for retrieval quality."""
    header = [
        f"Repository: {repo_slug}",
        f"Branch: {branch}",
        f"Commit SHA: {head_sha}",
        f"Path: {relative_path}",
        "",
    ]
    language = infer_language(Path(relative_path))
    if language:
        return "\n".join(header) + f"```{language}\n{source_text.rstrip()}\n```\n"
    return "\n".join(header) + source_text.rstrip() + "\n"


def make_inline_attributes(
    relative_path: str,
    repo_slug: str,
    branch: str,
    head_sha: str,
) -> list[dict[str, object]]:
    """Build inline metadata attributes for a custom Bedrock document."""
    path = Path(relative_path)
    attrs: list[dict[str, object]] = [
        {"key": "repo", "value": {"type": "STRING", "stringValue": repo_slug}},
        {"key": "branch", "value": {"type": "STRING", "stringValue": branch}},
        {"key": "commit_sha", "value": {"type": "STRING", "stringValue": head_sha}},
        {"key": "path", "value": {"type": "STRING", "stringValue": relative_path}},
        {"key": "filename", "value": {"type": "STRING", "stringValue": path.name}},
        {"key": "content_type", "value": {"type": "STRING", "stringValue": "source-code"}},
    ]
    if path.suffix:
        attrs.append(
            {"key": "extension", "value": {"type": "STRING", "stringValue": path.suffix.lower()}},
        )
    if path.parts:
        attrs.append(
            {"key": "top_level_dir", "value": {"type": "STRING", "stringValue": path.parts[0]}},
        )
    language = infer_language(path)
    if language:
        attrs.append(
            {"key": "language", "value": {"type": "STRING", "stringValue": language}},
        )
    return attrs


def build_document_id(relative_path: str, branch: str) -> str:
    """Create a stable document ID scoped to a branch and path."""
    return f"{branch}:{relative_path}"


def build_custom_document(
    path: Path,
    root: Path,
    repo_slug: str,
    branch: str,
    head_sha: str,
) -> CuratedDocument:
    """Transform a staged source file into an inline custom KB document."""
    relative_path = path.relative_to(root).as_posix()
    raw_text = path.read_text(encoding="utf-8", errors="replace")
    text = make_document_text(relative_path, raw_text, repo_slug, branch, head_sha)
    document_id = build_document_id(relative_path, branch)
    payload = {
        "content": {
            "dataSourceType": "CUSTOM",
            "custom": {
                "customDocumentIdentifier": {"id": document_id},
                "inlineContent": {
                    "type": "TEXT",
                    "textContent": {"data": text},
                },
                "sourceType": "IN_LINE",
            },
        },
        "metadata": {
            "type": "IN_LINE_ATTRIBUTE",
            "inlineAttributes": make_inline_attributes(
                relative_path,
                repo_slug,
                branch,
                head_sha,
            ),
        },
    }
    return CuratedDocument(
        document_id=document_id,
        relative_path=relative_path,
        text_bytes=len(text.encode("utf-8")),
        payload=payload,
    )


def iter_custom_documents(
    root: Path,
    repo_slug: str,
    branch: str,
    head_sha: str,
) -> Iterator[CuratedDocument]:
    """Yield curated files as inline custom KB documents."""
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.parts[-2:] == ("_metadata", "manifest.json"):
            continue
        yield build_custom_document(path, root, repo_slug, branch, head_sha)


def chunk_documents(documents: Iterable[CuratedDocument]) -> Iterator[list[CuratedDocument]]:
    """Batch custom documents within Bedrock direct-ingestion limits."""
    batch: list[CuratedDocument] = []
    batch_bytes = 0
    for document in documents:
        estimated_bytes = len(json.dumps(document.payload).encode("utf-8"))
        if batch and (
            len(batch) >= CUSTOM_BATCH_DOC_LIMIT
            or batch_bytes + estimated_bytes > CUSTOM_BATCH_BYTE_LIMIT
        ):
            yield batch
            batch = []
            batch_bytes = 0

        batch.append(document)
        batch_bytes += estimated_bytes

    if batch:
        yield batch


def find_data_source_id(agent_client: Any, knowledge_base_id: str, name: str) -> str | None:
    """Look up a data source by name."""
    paginator = agent_client.get_paginator("list_data_sources")
    for page in paginator.paginate(knowledgeBaseId=knowledge_base_id):
        for summary in page.get("dataSourceSummaries", []):
            if summary["name"] == name:
                return summary["dataSourceId"]
    return None


def wait_for_data_source(agent_client: Any, knowledge_base_id: str, data_source_id: str) -> None:
    """Wait until a data source becomes available."""
    deadline = time.monotonic() + DATA_SOURCE_POLL_TIMEOUT_SECONDS
    while True:
        response = agent_client.get_data_source(
            knowledgeBaseId=knowledge_base_id,
            dataSourceId=data_source_id,
        )
        status = response["dataSource"]["status"]
        if status == "AVAILABLE":
            return
        if status in {"DELETE_UNSUCCESSFUL", "FAILED"}:
            raise RuntimeError(
                f"Data source {data_source_id} entered terminal status {status}.",
            )
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"Timed out waiting for data source {data_source_id} to become AVAILABLE.",
            )
        logger.info(
            "Waiting for data source %s to become AVAILABLE. Current status: %s",
            data_source_id,
            status,
        )
        time.sleep(DATA_SOURCE_POLL_INTERVAL_SECONDS)


def is_document_operation_limit_error(exc: ClientError) -> bool:
    """Return whether Bedrock rejected the request due to concurrent limits."""
    error = exc.response.get("Error", {})
    return error.get("Code") == "ValidationException" and (
        "concurrent IngestKnowledgeBaseDocuments and DeleteKnowledgeBaseDocuments requests"
        in error.get("Message", "")
    )


def is_immutable_web_parsing_error(exc: ClientError) -> bool:
    """Return whether Bedrock refused to mutate an existing web parsing config."""
    error = exc.response.get("Error", {})
    return error.get("Code") == "ValidationException" and (
        "vectorIngestionConfiguration.parsingConfiguration cannot be updated once created"
        in error.get("Message", "")
    )


def call_with_document_retry(operation: Any, description: str) -> Any:
    """Retry document operations when account concurrency is exhausted."""
    for attempt in range(1, DOCUMENT_OPERATION_RETRY_LIMIT + 1):
        try:
            return operation()
        except ClientError as exc:
            if not is_document_operation_limit_error(exc) or attempt == DOCUMENT_OPERATION_RETRY_LIMIT:
                raise
            delay_seconds = min(DOCUMENT_OPERATION_RETRY_BASE_SECONDS * attempt, 60)
            logger.warning(
                "Concurrent Bedrock document limit hit during %s. Retrying in %s seconds (%s/%s).",
                description,
                delay_seconds,
                attempt,
                DOCUMENT_OPERATION_RETRY_LIMIT,
            )
            time.sleep(delay_seconds)
    raise RuntimeError(f"Exceeded retry budget for {description}.")


def ensure_code_data_source(agent_client: Any, args: RefreshArgs) -> str:
    """Create or update the custom data source used for source code."""
    data_source_id = find_data_source_id(
        agent_client,
        args.code_kb_id,
        args.code_data_source_name,
    )
    payload = {
        "knowledgeBaseId": args.code_kb_id,
        "name": args.code_data_source_name,
        "dataDeletionPolicy": "RETAIN",
        "dataSourceConfiguration": {
            "type": "CUSTOM",
        },
        "vectorIngestionConfiguration": {
            "chunkingConfiguration": {
                "chunkingStrategy": "FIXED_SIZE",
                "fixedSizeChunkingConfiguration": {
                    "maxTokens": FIXED_CHUNK_TOKENS,
                    "overlapPercentage": FIXED_CHUNK_OVERLAP,
                },
            },
        },
    }
    if data_source_id:
        agent_client.update_data_source(dataSourceId=data_source_id, **payload)
        wait_for_data_source(agent_client, args.code_kb_id, data_source_id)
        return data_source_id

    response = agent_client.create_data_source(
        clientToken=make_client_token("valkey-code"),
        **payload,
    )
    data_source_id = response["dataSource"]["dataSourceId"]
    wait_for_data_source(agent_client, args.code_kb_id, data_source_id)
    return data_source_id


def ensure_web_data_source(agent_client: Any, args: RefreshArgs) -> str:
    """Create or update the Valkey docs web crawler data source."""
    data_source_id = args.web_data_source_id or find_data_source_id(
        agent_client,
        args.web_kb_id,
        args.web_data_source_name,
    )
    payload = {
        "knowledgeBaseId": args.web_kb_id,
        "name": args.web_data_source_name,
        "dataDeletionPolicy": "DELETE",
        "dataSourceConfiguration": {
            "type": "WEB",
            "webConfiguration": {
                "crawlerConfiguration": {
                    "crawlerLimits": {
                        "rateLimit": 300,
                    },
                    "scope": "HOST_ONLY",
                },
                "sourceConfiguration": {
                    "urlConfiguration": {
                        "seedUrls": [{"url": url} for url in args.web_seed_urls],
                    },
                },
            },
        },
        "vectorIngestionConfiguration": {
            "parsingConfiguration": {
                "parsingStrategy": "BEDROCK_DATA_AUTOMATION",
            },
        },
    }
    if data_source_id:
        try:
            agent_client.update_data_source(dataSourceId=data_source_id, **payload)
            wait_for_data_source(agent_client, args.web_kb_id, data_source_id)
            return data_source_id
        except ClientError as exc:
            if not is_immutable_web_parsing_error(exc):
                raise
            logger.warning(
                "Existing web data source %s cannot be updated in place because parsing configuration is immutable. "
                "Creating a dedicated replacement data source named %s.",
                data_source_id,
                args.web_data_source_name,
            )

    response = agent_client.create_data_source(
        clientToken=make_client_token("valkey-docs"),
        **payload,
    )
    data_source_id = response["dataSource"]["dataSourceId"]
    wait_for_data_source(agent_client, args.web_kb_id, data_source_id)
    return data_source_id


def list_existing_document_ids(
    agent_client: Any,
    knowledge_base_id: str,
    data_source_id: str,
) -> set[str]:
    """List existing custom document IDs for a data source."""
    document_ids: set[str] = set()
    next_token: str | None = None
    while True:
        kwargs: dict[str, str] = {
            "knowledgeBaseId": knowledge_base_id,
            "dataSourceId": data_source_id,
        }
        if next_token:
            kwargs["nextToken"] = next_token
        response = agent_client.list_knowledge_base_documents(**kwargs)
        for detail in response.get("documentDetails", []):
            identifier = detail.get("identifier", {})
            custom = identifier.get("custom")
            if custom and custom.get("id"):
                document_ids.add(custom["id"])
        next_token = response.get("nextToken")
        if not next_token:
            return document_ids


def delete_stale_documents(
    agent_client: Any,
    knowledge_base_id: str,
    data_source_id: str,
    document_ids: set[str],
) -> int:
    """Delete documents that no longer exist in the curated source set."""
    deleted = 0
    stale = sorted(document_ids)
    for index in range(0, len(stale), CUSTOM_BATCH_DOC_LIMIT):
        batch = stale[index:index + CUSTOM_BATCH_DOC_LIMIT]
        call_with_document_retry(
            lambda: agent_client.delete_knowledge_base_documents(
                knowledgeBaseId=knowledge_base_id,
                dataSourceId=data_source_id,
                clientToken=make_client_token(f"delete-{data_source_id}-{index}"),
                documentIdentifiers=[
                    {
                        "dataSourceType": "CUSTOM",
                        "custom": {"id": document_id},
                    }
                    for document_id in batch
                ],
            ),
            description=f"delete batch starting at offset {index}",
        )
        deleted += len(batch)
    return deleted


def ingest_custom_documents(
    agent_client: Any,
    knowledge_base_id: str,
    data_source_id: str,
    documents: Iterable[CuratedDocument],
) -> tuple[int, int]:
    """Upsert custom documents into the code knowledge base."""
    ingested = 0
    indexed = 0
    for batch_number, batch in enumerate(chunk_documents(documents), start=1):
        response = call_with_document_retry(
            lambda: agent_client.ingest_knowledge_base_documents(
                knowledgeBaseId=knowledge_base_id,
                dataSourceId=data_source_id,
                clientToken=make_client_token(f"ingest-{data_source_id}-{batch_number}"),
                documents=[document.payload for document in batch],
            ),
            description=f"ingest batch {batch_number}",
        )
        details = response.get("documentDetails", [])
        ingested += len(details)
        indexed += sum(
            1 for detail in details if detail.get("status") in TERMINAL_DOCUMENT_STATUSES
        )
        logger.info("Submitted batch %s with %s documents.", batch_number, len(batch))
    return ingested, indexed


def start_ingestion(
    agent_client: Any,
    knowledge_base_id: str,
    data_source_id: str,
    description: str,
) -> str:
    """Start a Bedrock ingestion job and return the job id."""
    response = agent_client.start_ingestion_job(
        knowledgeBaseId=knowledge_base_id,
        dataSourceId=data_source_id,
        description=description,
        clientToken=make_client_token(f"{knowledge_base_id}-{data_source_id}"),
    )
    return response["ingestionJob"]["ingestionJobId"]


def prepare_corpus(args: RefreshArgs) -> tuple[str, int, int, list[CuratedDocument]]:
    """Clone Valkey, curate source files, and build custom document payloads."""
    repo_slug = infer_repo_slug(args.repo_url)
    with tempfile.TemporaryDirectory(prefix="valkey-kb-refresh-") as tmp_dir:
        tmp_root = Path(tmp_dir)
        repo_root = tmp_root / "valkey"
        staged_root = tmp_root / "staged"
        head_sha = clone_repo(args.repo_url, args.branch, repo_root)
        file_count, total_bytes = stage_curated_corpus(
            repo_root,
            staged_root,
            args.repo_url,
            args.branch,
            head_sha,
        )
        logger.info(
            "Prepared curated corpus with %s files (%.2f MiB).",
            file_count,
            total_bytes / (1024 * 1024),
        )
        documents = list(iter_custom_documents(staged_root, repo_slug, args.branch, head_sha))
    return head_sha, file_count, total_bytes, documents


def refresh(args: RefreshArgs) -> dict[str, object]:
    """Refresh the source-code and docs knowledge bases."""
    session = boto3.Session(region_name=args.region)
    agent_client = session.client("bedrock-agent")
    repo_slug = infer_repo_slug(args.repo_url)
    head_sha, file_count, total_bytes, documents = prepare_corpus(args)
    logger.info("Prepared %s custom KB documents for direct ingestion.", len(documents))

    code_data_source_id = find_data_source_id(
        agent_client,
        args.code_kb_id,
        args.code_data_source_name,
    )
    web_data_source_id = None
    if not args.skip_web_sync:
        web_data_source_id = args.web_data_source_id or find_data_source_id(
            agent_client,
            args.web_kb_id,
            args.web_data_source_name,
        )

    result: dict[str, object] = {
        "repo": repo_slug,
        "branch": args.branch,
        "head_sha": head_sha,
        "code_file_count": file_count,
        "code_total_bytes": total_bytes,
        "code_document_count": len(documents),
        "code_data_source_id": code_data_source_id,
        "web_data_source_id": web_data_source_id,
        "dry_run": args.dry_run,
    }

    if args.dry_run:
        result["code_data_source_action"] = (
            "update-existing" if code_data_source_id else "create"
        )
        if not args.skip_web_sync:
            result["web_data_source_action"] = (
                "update-existing" if web_data_source_id else "create"
            )
        return result

    code_data_source_id = ensure_code_data_source(agent_client, args)
    existing_ids = list_existing_document_ids(
        agent_client,
        args.code_kb_id,
        code_data_source_id,
    )
    desired_ids = {document.document_id for document in documents}
    documents_to_ingest = documents
    if args.missing_only:
        documents_to_ingest = [
            document for document in documents if document.document_id not in existing_ids
        ]
    deleted_documents = delete_stale_documents(
        agent_client,
        args.code_kb_id,
        code_data_source_id,
        existing_ids - desired_ids,
    )
    ingested_documents, acknowledged_documents = ingest_custom_documents(
        agent_client,
        args.code_kb_id,
        code_data_source_id,
        documents_to_ingest,
    )

    result.update({
        "code_data_source_id": code_data_source_id,
        "code_documents_requested": len(documents_to_ingest),
        "code_documents_submitted": ingested_documents,
        "code_documents_acknowledged": acknowledged_documents,
        "code_documents_deleted": deleted_documents,
    })

    if not args.skip_web_sync:
        web_data_source_id = ensure_web_data_source(agent_client, args)
        web_ingestion_job_id = start_ingestion(
            agent_client,
            args.web_kb_id,
            web_data_source_id,
            f"Refresh Valkey docs crawl at {datetime.now(timezone.utc).isoformat()}",
        )
        result["web_data_source_id"] = web_data_source_id
        result["web_ingestion_job_id"] = web_ingestion_job_id

    return result


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint."""
    args = parse_args(argv)
    configure_logging(args.verbose)
    try:
        result = refresh(args)
    except (ClientError, RuntimeError, subprocess.CalledProcessError, TimeoutError) as exc:
        logger.error("Refresh failed: %s", exc)
        return 1

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
