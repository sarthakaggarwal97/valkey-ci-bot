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
