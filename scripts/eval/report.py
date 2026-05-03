from __future__ import annotations

from scripts.eval.flow_scorer import FlowScore


def render_report(scores: list[FlowScore]) -> str:
    lines = ["# CI Agent Eval Report\n"]
    flows = sorted(set(s.flow for s in scores))
    lines.append("## Summary\n")
    lines.append("| Flow | Fixtures | Avg Correctness | Avg Style | Pass Rate |")
    lines.append("|------|----------|-----------------|-----------|-----------|")
    for flow in flows:
        fs = [s for s in scores if s.flow == flow]
        ac = sum(s.correctness for s in fs) / len(fs)
        ast = sum(s.style for s in fs) / len(fs)
        pr = sum(1 for s in fs if s.correctness >= 0.5) / len(fs)
        lines.append(f"| {flow} | {len(fs)} | {ac:.2f} | {ast:.2f} | {pr:.0%} |")
    lines.append("\n## Per-Fixture Results\n")
    for flow in flows:
        lines.append(f"### {flow.title()} Flow\n")
        lines.append("| Fixture | Correctness | Style | Details |")
        lines.append("|---------|-------------|-------|---------|")
        for s in sorted((s for s in scores if s.flow == flow), key=lambda s: s.correctness):
            ds = ", ".join(f"{k}={v}" for k, v in s.details.items()
                          if k not in ("agent_files", "truth_files", "overlap", "note"))
            e = "\u2705" if s.correctness >= 0.5 else "\u274c"
            lines.append(f"| {e} {s.fixture_name} | {s.correctness:.2f} | {s.style:.2f} | {ds} |")
        lines.append("")
    divergences = sorted(scores, key=lambda s: s.correctness)[:5]
    if divergences and divergences[0].correctness < 0.5:
        lines.append("## Top Divergences (agent vs ground truth)\n")
        for s in divergences:
            if s.correctness >= 0.5:
                break
            lines.append(f"- **{s.fixture_name}** ({s.flow}): correctness={s.correctness:.2f}")
            for k, v in s.details.items():
                lines.append(f"  - {k}: {v}")
            lines.append("")
    return "\n".join(lines)
