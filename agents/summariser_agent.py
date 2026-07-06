"""Agent 2: reads JMeter statistics.json per thread level and writes a markdown summary per API."""

import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

if __name__ == "__main__" and __package__ is None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.common import PROJECT_ROOT, get_llm, load_config, setup_logging

logger = setup_logging(__name__)

REPORTS_DIR: Path = PROJECT_ROOT / "results" / "reports"
SUMMARIES_DIR: Path = PROJECT_ROOT / "results" / "summaries"

BREAKING_POINT_ERROR_PCT: float = 5.0

ANALYSIS_PROMPT = """You are a performance test analyst. Given the staircase load test \
results below for the API "{api_name}", write two short sections in plain prose (no headings):

1. Key Findings: at which thread count errors first appeared, the error type pattern based on \
how error % trends as threads increase, and any notable latency behaviour.
2. Recommendation: one sentence recommending a safe concurrency limit for production use.

Results (threads, requests, avg_ms, min_ms, max_ms, error_pct):
{rows}

Respond with exactly two paragraphs: the first for Key Findings, the second for Recommendation. \
Do not include headings or markdown formatting.
"""


def _status_for_error_pct(error_pct: float) -> str:
    """Map an error percentage to a health status label.

    Args:
        error_pct: Error percentage for a thread level.

    Returns:
        One of "✅ Healthy", "⚠️ Degrading", "💀 Critical" per CLAUDE.md thresholds.
    """
    if error_pct < 1.0:
        return "✅ Healthy"
    if error_pct <= 10.0:
        return "⚠️ Degrading"
    return "💀 Critical"


def collect_stats_for_api(api_name: str, thread_levels: list[int]) -> list[dict[str, Any]]:
    """Read statistics.json for each thread level of an API, skipping missing files.

    Args:
        api_name: Name of the API as defined in config.yaml.
        thread_levels: List of thread counts tested in the staircase.

    Returns:
        List of row dicts with threads, sampleCount, meanResTime, minResTime, maxResTime, errorPct.
    """
    rows: list[dict[str, Any]] = []
    for threads in thread_levels:
        stats_path = REPORTS_DIR / api_name / f"threads_{threads}" / "statistics.json"
        if not stats_path.exists():
            logger.warning("statistics.json not found for %s at threads=%s, skipping", api_name, threads)
            continue

        try:
            with open(stats_path, encoding="utf-8") as f:
                stats = json.load(f)
            total = stats["Total"]
        except (json.JSONDecodeError, KeyError, OSError) as exc:
            logger.warning("Could not parse statistics.json for %s at threads=%s: %s", api_name, threads, exc)
            continue

        rows.append(
            {
                "threads": threads,
                "sampleCount": total.get("sampleCount", 0),
                "meanResTime": total.get("meanResTime", 0.0),
                "minResTime": total.get("minResTime", 0.0),
                "maxResTime": total.get("maxResTime", 0.0),
                "errorPct": total.get("errorPct", 0.0),
            }
        )
    return rows


def find_breaking_point(rows: list[dict[str, Any]]) -> tuple[int | None, int | None]:
    """Determine the safe concurrency limit and the breaking point from stat rows.

    Args:
        rows: Per-thread-level stat rows as returned by collect_stats_for_api.

    Returns:
        Tuple of (safe_limit, breaking_point) thread counts. Either may be None if
        no rows are available or no level breached BREAKING_POINT_ERROR_PCT.
    """
    if not rows:
        return None, None

    safe_limit = None
    breaking_point = None
    for row in rows:
        if row["errorPct"] > BREAKING_POINT_ERROR_PCT:
            breaking_point = row["threads"]
            break
        safe_limit = row["threads"]

    return safe_limit, breaking_point


def _fallback_analysis(rows: list[dict[str, Any]], safe_limit: int | None, breaking_point: int | None) -> tuple[str, str]:
    """Produce a template-based (non-LLM) Key Findings / Recommendation pair.

    Used when the LLM call fails, per CLAUDE.md's "fall back to template-based summary" rule.

    Args:
        rows: Per-thread-level stat rows.
        safe_limit: Highest thread count with error % at or below BREAKING_POINT_ERROR_PCT.
        breaking_point: First thread count exceeding BREAKING_POINT_ERROR_PCT, if any.

    Returns:
        Tuple of (key_findings, recommendation) plain-text strings.
    """
    first_error_row = next((r for r in rows if r["errorPct"] > 0), None)
    if first_error_row:
        findings = (
            f"Errors first appeared at {first_error_row['threads']} concurrent threads "
            f"({first_error_row['errorPct']:.2f}% error rate). "
        )
    else:
        findings = "No errors were observed across any tested thread level. "

    if breaking_point:
        findings += f"Error rate exceeded {BREAKING_POINT_ERROR_PCT}% at {breaking_point} threads."
    else:
        findings += "Error rate stayed within acceptable bounds across all tested thread levels."

    if safe_limit and breaking_point:
        recommendation = (
            f"Recommend keeping production concurrency at or below {safe_limit} threads; "
            f"{breaking_point} threads was the observed breaking point."
        )
    elif safe_limit:
        recommendation = f"Recommend keeping production concurrency at or below {safe_limit} threads."
    else:
        recommendation = "Insufficient data to recommend a safe concurrency limit."

    return findings, recommendation


def analyse_with_llm(config: dict[str, Any], api_name: str, rows: list[dict[str, Any]]) -> tuple[str, str]:
    """Ask the configured LLM to analyse stat rows, falling back to a template on failure.

    Args:
        config: Parsed config.yaml dictionary.
        api_name: Name of the API being summarised.
        rows: Per-thread-level stat rows (numbers only — no raw response bodies).

    Returns:
        Tuple of (key_findings, recommendation) strings.
    """
    safe_limit, breaking_point = find_breaking_point(rows)

    try:
        llm = get_llm(config)
        rows_text = "\n".join(
            f"- {r['threads']} threads: {r['sampleCount']} requests, avg={r['meanResTime']}ms, "
            f"min={r['minResTime']}ms, max={r['maxResTime']}ms, error={r['errorPct']}%"
            for r in rows
        )
        prompt = ANALYSIS_PROMPT.format(api_name=api_name, rows=rows_text)
        response = llm.invoke(prompt)
        text = response.content if hasattr(response, "content") else str(response)
        paragraphs = [p.strip() for p in text.strip().split("\n\n") if p.strip()]
        if len(paragraphs) >= 2:
            return paragraphs[0], paragraphs[1]
        logger.warning("LLM response for %s was not in the expected two-paragraph format, using fallback", api_name)
    except Exception as exc:  # noqa: BLE001 - any LLM failure must fall back, never crash
        logger.warning("LLM analysis failed for %s: %s — using template fallback", api_name, exc)

    return _fallback_analysis(rows, safe_limit, breaking_point)


def render_summary_md(api_config: dict[str, Any], rows: list[dict[str, Any]], findings: str, recommendation: str) -> str:
    """Render the per-API markdown summary document.

    Args:
        api_config: One API entry from config.yaml.
        rows: Per-thread-level stat rows.
        findings: Key Findings paragraph.
        recommendation: Recommendation paragraph.

    Returns:
        Full markdown document as a string.
    """
    safe_limit, breaking_point = find_breaking_point(rows)
    breaking_row = next((r for r in rows if r["threads"] == breaking_point), None)
    breaking_pct = f"{breaking_row['errorPct']:.2f}%" if breaking_row else "N/A"

    table_rows = "\n".join(
        f"| {r['threads']} | {r['sampleCount']} | {r['meanResTime']:.0f} | {r['minResTime']:.0f} | "
        f"{r['maxResTime']:.0f} | {r['errorPct']:.2f}% | {_status_for_error_pct(r['errorPct'])} |"
        for r in rows
    )

    return f"""# API Stress Test Summary: {api_config['name']}

**URL:** {api_config['url']}
**Method:** {api_config.get('method', 'GET')}
**Test Date:** {date.today().isoformat()}

## Results Table

| Threads | Requests | Avg (ms) | Min (ms) | Max (ms) | Error % | Status |
| ------- | -------- | -------- | -------- | -------- | ------- | ------ |
{table_rows}

## Key Findings

{findings}

## Breaking Point

**Safe limit:** {safe_limit if safe_limit else 'N/A'} concurrent users
**Breaking point:** {breaking_point if breaking_point else 'Not reached'} concurrent users ({breaking_pct} error rate)

## Recommendation

{recommendation}
"""


def summarise_api(config: dict[str, Any], api_config: dict[str, Any]) -> None:
    """Build and write the markdown summary for a single API.

    Args:
        config: Parsed config.yaml dictionary.
        api_config: One API entry from config.yaml.
    """
    api_name = api_config["name"]
    thread_levels: list[int] = config["settings"]["thread_levels"]

    rows = collect_stats_for_api(api_name, thread_levels)
    if not rows:
        logger.warning("No statistics available for %s, skipping summary", api_name)
        return

    findings, recommendation = analyse_with_llm(config, api_name, rows)
    markdown = render_summary_md(api_config, rows, findings, recommendation)

    SUMMARIES_DIR.mkdir(parents=True, exist_ok=True)
    summary_path = SUMMARIES_DIR / f"{api_name}.md"
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write(markdown)
    logger.info("Wrote summary for %s to %s", api_name, summary_path)


def main() -> None:
    """Entry point: summarise every API defined in config.yaml."""
    config = load_config()
    for api_config in config["apis"]:
        try:
            summarise_api(config, api_config)
        except Exception as exc:  # noqa: BLE001 - one API's summary failure must not stop the rest
            logger.error("Failed to summarise %s: %s", api_config["name"], exc)


if __name__ == "__main__":
    main()
