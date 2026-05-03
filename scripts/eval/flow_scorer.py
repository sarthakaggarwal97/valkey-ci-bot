from __future__ import annotations

from dataclasses import dataclass, field

from scripts.eval.eval_scorer import score_style


@dataclass
class FlowScore:
    flow: str
    fixture_name: str
    correctness: float
    style: float
    details: dict = field(default_factory=dict)


def score_review_flow(
    fixture_name: str,
    agent_findings: list[dict],
    maintainer_comments: list[dict],
    *,
    line_tolerance: int = 5,
) -> FlowScore:
    if not maintainer_comments:
        return FlowScore(
            flow="review", fixture_name=fixture_name,
            correctness=1.0 if not agent_findings else 0.5,
            style=score_style(agent_findings),
            details={"note": "no maintainer comments to compare against"},
        )
    matched_maintainer: set[int] = set()
    matched_agent: set[int] = set()
    for i, af in enumerate(agent_findings):
        for j, mc in enumerate(maintainer_comments):
            if j in matched_maintainer:
                continue
            if af.get("path") == mc.get("path"):
                al = af.get("line", 0) or 0
                ml = mc.get("line", 0) or 0
                if abs(al - ml) <= line_tolerance:
                    matched_maintainer.add(j)
                    matched_agent.add(i)
                    break
    tp = len(matched_agent)
    fp = len(agent_findings) - tp
    fn = len(maintainer_comments) - len(matched_maintainer)
    prec = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    rec = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) > 0 else 0.0
    return FlowScore(
        flow="review", fixture_name=fixture_name,
        correctness=round(f1, 3), style=score_style(agent_findings),
        details={"precision": round(prec, 3), "recall": round(rec, 3),
                 "true_positives": tp, "false_positives": fp, "missed": fn,
                 "agent_count": len(agent_findings), "maintainer_count": len(maintainer_comments)},
    )


def score_review_flow_loose(
    fixture_name: str,
    agent_findings: list[dict],
    maintainer_comments: list[dict],
    *,
    line_tolerance: int = 10,
) -> FlowScore:
    """Loosened review scoring.

    Credits partial matches where the agent flagged an issue in a file that
    maintainers commented on, even if the line is off by more than ``line_tolerance``.
    This captures cases where the agent finds a related-but-not-colocated issue.

    Weights:
      - Line match (within ``line_tolerance``): 1.0 per pair (same as strict F1)
      - File match only: 0.5 per pair
      - No match: 0.0

    Returns an F1-like score where TP counts use these weights.
    """
    if not maintainer_comments:
        return FlowScore(
            flow="review", fixture_name=fixture_name,
            correctness=1.0 if not agent_findings else 0.5,
            style=score_style(agent_findings),
            details={"note": "no maintainer comments to compare against"},
        )

    exact_matched_maintainer: set[int] = set()
    exact_matched_agent: set[int] = set()
    file_matched_maintainer: set[int] = set()
    file_matched_agent: set[int] = set()

    # First pass: exact line matches
    for i, af in enumerate(agent_findings):
        for j, mc in enumerate(maintainer_comments):
            if j in exact_matched_maintainer:
                continue
            if af.get("path") == mc.get("path"):
                al = af.get("line", 0) or 0
                ml = mc.get("line", 0) or 0
                if abs(al - ml) <= line_tolerance:
                    exact_matched_maintainer.add(j)
                    exact_matched_agent.add(i)
                    break

    # Second pass: file-only matches among unmatched agent findings
    for i, af in enumerate(agent_findings):
        if i in exact_matched_agent:
            continue
        for j, mc in enumerate(maintainer_comments):
            if j in exact_matched_maintainer or j in file_matched_maintainer:
                continue
            if af.get("path") == mc.get("path"):
                file_matched_maintainer.add(j)
                file_matched_agent.add(i)
                break

    exact_tp = len(exact_matched_agent)
    file_tp_count = len(file_matched_agent)
    # Weighted true positives for numerator (file-only matches count half)
    weighted_tp = exact_tp + 0.5 * file_tp_count

    # Denominators use full counts (every finding / every comment is one "attempt")
    unmatched_agent = len(agent_findings) - exact_tp - file_tp_count
    unmatched_maint = len(maintainer_comments) - len(exact_matched_maintainer) - len(file_matched_maintainer)

    total_agent = weighted_tp + unmatched_agent + 0.5 * file_tp_count
    total_maint = weighted_tp + unmatched_maint + 0.5 * file_tp_count

    prec = weighted_tp / total_agent if total_agent > 0 else 0.0
    rec = weighted_tp / total_maint if total_maint > 0 else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) > 0 else 0.0

    return FlowScore(
        flow="review", fixture_name=fixture_name,
        correctness=round(f1, 3), style=score_style(agent_findings),
        details={
            "precision": round(prec, 3), "recall": round(rec, 3),
            "exact_matches": exact_tp, "file_only_matches": file_tp_count,
            "unmatched_agent": unmatched_agent, "unmatched_maintainer": unmatched_maint,
            "agent_count": len(agent_findings),
            "maintainer_count": len(maintainer_comments),
        },
    )


def score_daily_flow(fixture_name: str, agent_rca: dict, ground_truth: dict) -> FlowScore:
    af = set(agent_rca.get("files_to_change", []))
    tf = set(ground_truth.get("fix_files_changed", []))
    if not tf:
        return FlowScore(flow="daily", fixture_name=fixture_name, correctness=0.5, style=1.0,
                         details={"note": "no ground truth fix files"})
    inter = af & tf
    union = af | tf
    jaccard = len(inter) / len(union) if union else 0.0
    flaky_match = agent_rca.get("is_flaky", False) == ground_truth.get("is_flaky", False)
    correctness = (jaccard * 0.7) + (0.3 if flaky_match else 0.0)
    return FlowScore(
        flow="daily", fixture_name=fixture_name,
        correctness=round(correctness, 3), style=1.0,
        details={"jaccard": round(jaccard, 3), "flaky_match": flaky_match,
                 "agent_files": sorted(af), "truth_files": sorted(tf), "overlap": sorted(inter)},
    )


def score_backport_flow(fixture_name: str, agent_result: dict, ground_truth: dict) -> FlowScore:
    af = set(agent_result.get("files_changed", []))
    tf = set(ground_truth.get("files_changed", []))
    file_match = len(af & tf) / len(tf) if tf else (1.0 if not af else 0.0)
    has_empty = any(f.get("size", 1) == 0 for f in agent_result.get("file_details", []))
    if has_empty:
        file_match = 0.0
    return FlowScore(
        flow="backport", fixture_name=fixture_name,
        correctness=round(file_match, 3), style=1.0,
        details={"agent_files": sorted(af), "truth_files": sorted(tf), "has_empty_files": has_empty},
    )


def score_fuzzer_flow(fixture_name: str, agent_analysis: dict, ground_truth: dict) -> FlowScore:
    ac = agent_analysis.get("root_cause_category", "").lower()
    tc = ground_truth.get("root_cause_category", "").lower()
    cat_match = ac == tc or tc in ac or ac in tc
    af = set(agent_analysis.get("files_involved", []))
    tf = set(ground_truth.get("fix_files_changed", []))
    fo = len(af & tf) / len(tf) if tf else 0.5
    correctness = (0.5 if cat_match else 0.0) + (fo * 0.5)
    return FlowScore(
        flow="fuzzer", fixture_name=fixture_name,
        correctness=round(correctness, 3), style=1.0,
        details={"category_match": cat_match, "agent_category": ac,
                 "truth_category": tc, "file_overlap": round(fo, 3)},
    )
