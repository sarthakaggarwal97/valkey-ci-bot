from __future__ import annotations

from scripts.models import ReviewFinding
from scripts.review_diff import parse_diff_map, validate_review_finding_for_publish


def test_parse_diff_map_allows_added_and_context_lines_only() -> None:
    diff_map = parse_diff_map(
        "src/server.c",
        "\n".join([
            "@@ -10,3 +10,4 @@",
            " context",
            "-old line",
            "+new line",
            " more context",
        ]),
    )

    assert diff_map.is_commentable(10)
    assert diff_map.is_commentable(11)
    assert diff_map.is_commentable(12)
    assert not diff_map.is_commentable(9)


def test_parse_diff_map_ignores_no_newline_marker() -> None:
    """`\\ No newline at end of file` markers must not advance new_line.

    Regression: previously the marker fell into the context branch and
    bumped new_line, pushing every subsequent hunk's line numbers off by
    one for every marker present.
    """
    patch = "\n".join([
        "@@ -1,2 +1,2 @@",
        " context line",
        "-old tail",
        "+new tail",
        "\\ No newline at end of file",
        "@@ -10,1 +10,2 @@",
        " another context",
        "+appended",
    ])
    diff_map = parse_diff_map("src/a.c", patch)

    # First hunk: context at 1, added at 2. The marker must NOT become line 3.
    assert diff_map.added_lines == {2, 11}
    assert diff_map.context_lines == {1, 10}
    assert not diff_map.is_commentable(3)
    assert not diff_map.is_commentable(12)


def test_validate_review_finding_rejects_methodology_and_off_diff_paths() -> None:
    diff_maps = {"src/server.c": parse_diff_map("src/server.c", "@@ -1 +1 @@\n+new")}
    ok = ReviewFinding(
        path="src/server.c",
        line=1,
        severity="medium",
        title="Leak",
        body="This path leaks the allocated object on the error return.",
    )
    assert validate_review_finding_for_publish(ok, diff_maps) == (True, "")

    leaked_method = ReviewFinding(
        path="src/server.c",
        line=1,
        severity="medium",
        title="Method",
        body="I ran git grep and found this.",
    )
    assert validate_review_finding_for_publish(leaked_method, diff_maps)[0] is False

    wrong_path = ReviewFinding(
        path="src/other.c",
        line=1,
        severity="medium",
        title="Path",
        body="This comment targets a file that is not in the diff.",
    )
    assert validate_review_finding_for_publish(wrong_path, diff_maps)[1] == "path-not-in-diff"
