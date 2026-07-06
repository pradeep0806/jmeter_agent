"""Agent 2: reads JMeter statistics.json per thread level and writes a markdown summary per API."""

import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any, Literal

if __name__ == "__main__" and __package__ is None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pydantic import BaseModel, ValidationError

from agents import metrics_store
from agents.common import (
    PROJECT_ROOT,
    get_llm,
    get_run_id,
    load_config,
    read_thread_level_stats,
    setup_logging,
)

logger = setup_logging(__name__)

REPORTS_DIR: Path = PROJECT_ROOT / "results" / "reports"
SUMMARIES_DIR: Path = PROJECT_ROOT / "results" / "summaries"

DEFAULT_BREAKING_POINT_ERROR_PCT: float = 5.0
DEFAULT_REGRESSION_THRESHOLD_PCT: float = 20.0

ANALYSIS_PROMPT = """You are a performance test analyst. Given the staircase load test \
results below for the API "{api_name}", respond with ONLY a JSON object matching exactly \
this schema (no markdown fences, no extra text):

{{"verdict": "healthy" | "degrading" | "critical", \
"bottleneck_hypothesis": "<one or two sentences on what is most likely causing errors/latency \
to rise as concurrency increases, based on the trend in the numbers below>", \
"recommendation": "<one sentence recommending a safe concurrency limit for production use>"}}

Results (threads, requests, avg_ms, p95_ms, p99_ms, throughput_rps, error_pct):
{rows}
"""


class ApiAnalysis(BaseModel):
    """Schema-validated LLM output for a single API's staircase analysis."""

    verdict: Literal["healthy", "degrading", "critical"]
    bottleneck_hypothesis: str
    recommendation: str


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
        List of row dicts (threads, sampleCount, meanResTime, minResTime, maxResTime,
        errorPct, p95, p99, throughput) — one that were actually tested (early-stop in
        runner_agent may mean higher levels were never run).
    """
    rows: list[dict[str, Any]] = []
    for threads in thread_levels:
        stats = read_thread_level_stats(REPORTS_DIR, api_name, threads)
        if stats is not None:
            rows.append(stats)
    return rows


def find_breaking_point(rows: list[dict[str, Any]], breaking_point_error_pct: float) -> tuple[int | None, int | None]:
    """Determine the safe concurrency limit and the breaking point from stat rows.

    Args:
        rows: Per-thread-level stat rows as returned by collect_stats_for_api.
        breaking_point_error_pct: Error % above which a level counts as the breaking point.

    Returns:
        Tuple of (safe_limit, breaking_point) thread counts. Either may be None if
        no rows are available or no level breached the threshold.
    """
    if not rows:
        return None, None

    safe_limit = None
    breaking_point = None
    for row in rows:
        if row["errorPct"] > breaking_point_error_pct:
            breaking_point = row["threads"]
            break
        safe_limit = row["threads"]

    return safe_limit, breaking_point


def _fallback_analysis(
    rows: list[dict[str, Any]], safe_limit: int | None, breaking_point: int | None, breaking_point_error_pct: float
) -> dict[str, str]:
    """Produce a template-based (non-LLM) verdict/bottleneck/recommendation triple.

    Used when the LLM call fails or returns invalid JSON, per CLAUDE.md's
    "fall back to template-based summary" rule.

    Args:
        rows: Per-thread-level stat rows.
        safe_limit: Highest thread count at or below the breaking-point threshold.
        breaking_point: First thread count exceeding the breaking-point threshold, if any.
        breaking_point_error_pct: Error % threshold used to compute safe_limit/breaking_point.

    Returns:
        Dict with "verdict", "bottleneck_hypothesis", "recommendation" keys.
    """
    max_error_pct = max((r["errorPct"] for r in rows), default=0.0)
    if max_error_pct < 1.0:
        verdict = "healthy"
    elif max_error_pct <= 10.0:
        verdict = "degrading"
    else:
        verdict = "critical"

    if breaking_point:
        bottleneck_hypothesis = (
            f"Error rate exceeds {breaking_point_error_pct}% at {breaking_point} concurrent threads, "
            f"consistent with the backend saturating its connection or thread pool around that concurrency."
        )
    else:
        bottleneck_hypothesis = "No clear bottleneck observed within the tested thread levels."

    if safe_limit and breaking_point:
        recommendation = (
            f"Recommend keeping production concurrency at or below {safe_limit} threads; "
            f"{breaking_point} threads was the observed breaking point."
        )
    elif safe_limit:
        recommendation = f"Recommend keeping production concurrency at or below {safe_limit} threads."
    else:
        recommendation = "Insufficient data to recommend a safe concurrency limit."

    return {
        "verdict": verdict,
        "bottleneck_hypothesis": bottleneck_hypothesis,
        "recommendation": recommendation,
    }


def _strip_code_fences(text: str) -> str:
    """Strip a leading/trailing ```json ... ``` fence from an LLM response, if present.

    Args:
        text: Raw LLM response text.

    Returns:
        Text with any surrounding markdown code fence removed.
    """
    stripped = text.strip()
    match = re.match(r"^```(?:json)?\s*(.*?)\s*```$", stripped, re.DOTALL)
    return match.group(1) if match else stripped


def analyse_with_llm(
    config: dict[str, Any], api_name: str, rows: list[dict[str, Any]], breaking_point_error_pct: float
) -> dict[str, str]:
    """Ask the configured LLM for a schema-validated analysis, falling back to a template.

    Args:
        config: Parsed config.yaml dictionary.
        api_name: Name of the API being summarised.
        rows: Per-thread-level stat rows (numbers only — no raw response bodies).
        breaking_point_error_pct: Error % threshold used by the template fallback.

    Returns:
        Dict with "verdict", "bottleneck_hypothesis", "recommendation" keys.
    """
    safe_limit, breaking_point = find_breaking_point(rows, breaking_point_error_pct)

    try:
        llm = get_llm(config)
        rows_text = "\n".join(
            f"- {r['threads']} threads: {r['sampleCount']} requests, avg={r['meanResTime']}ms, "
            f"p95={r['p95']}ms, p99={r['p99']}ms, throughput={r['throughput']}rps, error={r['errorPct']}%"
            for r in rows
        )
        prompt = ANALYSIS_PROMPT.format(api_name=api_name, rows=rows_text)
        response = llm.invoke(prompt)
        text = response.content if hasattr(response, "content") else str(response)
        analysis = ApiAnalysis.model_validate_json(_strip_code_fences(text))
        return analysis.model_dump()
    except (ValidationError, json.JSONDecodeError) as exc:
        logger.warning("LLM response for %s failed schema validation: %s — using template fallback", api_name, exc)
    except Exception as exc:  # noqa: BLE001 - any LLM failure must fall back, never crash
        logger.warning("LLM analysis failed for %s: %s — using template fallback", api_name, exc)

    return _fallback_analysis(rows, safe_limit, breaking_point, breaking_point_error_pct)


def render_summary_md(
    api_config: dict[str, Any],
    rows: list[dict[str, Any]],
    analysis: dict[str, str],
    breaking_point_error_pct: float,
    regression_flags: list[str],
) -> str:
    """Render the per-API markdown summary document.

    Args:
        api_config: One API entry from config.yaml.
        rows: Per-thread-level stat rows.
        analysis: Dict with "verdict", "bottleneck_hypothesis", "recommendation".
        breaking_point_error_pct: Error % threshold used to compute safe_limit/breaking_point.
        regression_flags: Human-readable regression descriptions vs the previous MLflow run.

    Returns:
        Full markdown document as a string.
    """
    safe_limit, breaking_point = find_breaking_point(rows, breaking_point_error_pct)
    breaking_row = next((r for r in rows if r["threads"] == breaking_point), None)
    breaking_pct = f"{breaking_row['errorPct']:.2f}%" if breaking_row else "N/A"

    table_rows = "\n".join(
        f"| {r['threads']} | {r['sampleCount']} | {r['meanResTime']:.0f} | {r['minResTime']:.0f} | "
        f"{r['maxResTime']:.0f} | {r['p95']:.0f} | {r['p99']:.0f} | {r['throughput']:.1f} | "
        f"{r['errorPct']:.2f}% | {_status_for_error_pct(r['errorPct'])} |"
        for r in rows
    )

    if regression_flags:
        regression_section = "\n".join(f"- {flag}" for flag in regression_flags)
    else:
        regression_section = "No regressions detected vs the previous run."

    return f"""# API Stress Test Summary: {api_config['name']}

**URL:** {api_config['url']}
**Method:** {api_config.get('method', 'GET')}
**Test Date:** {date.today().isoformat()}
**Verdict:** {analysis['verdict']}

## Results Table

| Threads | Requests | Avg (ms) | Min (ms) | Max (ms) | P95 (ms) | P99 (ms) | Throughput (rps) | Error % | Status |
| ------- | -------- | -------- | -------- | -------- | -------- | -------- | ----------------- | ------- | ------ |
{table_rows}

## Regression vs Previous Run

{regression_section}

## Bottleneck Hypothesis

{analysis['bottleneck_hypothesis']}

## Breaking Point

**Safe limit:** {safe_limit if safe_limit else 'N/A'} concurrent users
**Breaking point:** {breaking_point if breaking_point else 'Not reached'} concurrent users ({breaking_pct} error rate)

## Recommendation

{analysis['recommendation']}
"""


def summarise_api(config: dict[str, Any], api_config: dict[str, Any]) -> None:
    """Build and write the markdown summary for a single API.

    Args:
        config: Parsed config.yaml dictionary.
        api_config: One API entry from config.yaml.
    """
    api_name = api_config["name"]
    thread_levels: list[int] = config["settings"]["thread_levels"]
    breaking_point_error_pct: float = config["settings"].get(
        "breaking_point_error_pct", DEFAULT_BREAKING_POINT_ERROR_PCT
    )
    regression_threshold_pct: float = config["settings"].get(
        "regression_threshold_pct", DEFAULT_REGRESSION_THRESHOLD_PCT
    )

    rows = collect_stats_for_api(api_name, thread_levels)
    if not rows:
        logger.warning("No statistics available for %s, skipping summary", api_name)
        return

    run_id = get_run_id()
    previous_metrics = metrics_store.get_previous_run_metrics(config, api_name, run_id)
    regression_flags = metrics_store.detect_regressions(rows, previous_metrics, regression_threshold_pct)
    metrics_store.log_run_metrics(config, api_name, run_id, rows)

    analysis = analyse_with_llm(config, api_name, rows, breaking_point_error_pct)
    markdown = render_summary_md(api_config, rows, analysis, breaking_point_error_pct, regression_flags)

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
