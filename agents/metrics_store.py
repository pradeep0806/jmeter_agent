"""Persists per-run staircase metrics to MLflow and detects regressions against the previous run.

MLflow is used as the structured metrics store (per-thread-level metrics logged with
`step=threads`) so run history and comparison come from MLflow's own tracking store
rather than a hand-rolled database. All MLflow calls are best-effort: if MLflow is
unreachable, persistence/regression-checking is skipped and a warning is logged —
this must never crash the pipeline.
"""

import os
from typing import Any

from agents.common import setup_logging

logger = setup_logging(__name__)

METRIC_NAMES: tuple[str, ...] = ("error_pct", "mean_res_time", "p95", "p99", "throughput")


def _row_metric_value(row: dict[str, Any], metric: str) -> float:
    """Map a metric_store metric name to its value in a stats row.

    Args:
        row: A stats row as returned by common.read_thread_level_stats.
        metric: One of METRIC_NAMES.

    Returns:
        The corresponding numeric value from the row.
    """
    mapping = {
        "error_pct": "errorPct",
        "mean_res_time": "meanResTime",
        "p95": "p95",
        "p99": "p99",
        "throughput": "throughput",
    }
    return float(row[mapping[metric]])


def _configure_tracking_uri(config: dict[str, Any]) -> None:
    """Point MLflow at the configured tracking server, if any.

    Args:
        config: Parsed config.yaml dictionary.
    """
    import mlflow

    tracking_uri = os.getenv("MLFLOW_TRACKING_URI") or config["settings"].get("mlflow_tracking_uri")
    if tracking_uri:
        mlflow.set_tracking_uri(tracking_uri)


def log_run_metrics(config: dict[str, Any], api_name: str, run_id: str, rows: list[dict[str, Any]]) -> None:
    """Log one API's per-thread-level staircase metrics to MLflow as a single run.

    Args:
        config: Parsed config.yaml dictionary.
        api_name: Name of the API being tested; used as the MLflow experiment name.
        run_id: Pipeline correlation ID, attached as an MLflow run tag.
        rows: Per-thread-level stat rows (threads, errorPct, meanResTime, p95, p99, throughput).
    """
    try:
        import mlflow

        _configure_tracking_uri(config)
        mlflow.set_experiment(api_name)
        with mlflow.start_run(run_name=run_id):
            mlflow.set_tag("run_id", run_id)
            for row in rows:
                for metric in METRIC_NAMES:
                    mlflow.log_metric(metric, _row_metric_value(row, metric), step=row["threads"])
        logger.info("Logged %s thread levels for %s to MLflow", len(rows), api_name)
    except Exception as exc:  # noqa: BLE001 - MLflow being unreachable must not crash the pipeline
        logger.warning("Could not log metrics to MLflow for %s: %s", api_name, exc)


def get_previous_run_metrics(config: dict[str, Any], api_name: str, current_run_id: str) -> dict[int, dict[str, float]] | None:
    """Fetch the most recent completed MLflow run for an API, excluding the current run.

    Args:
        config: Parsed config.yaml dictionary.
        api_name: Name of the API being tested; the MLflow experiment name.
        current_run_id: This pipeline execution's run tag, excluded from the search.

    Returns:
        Dict of {threads: {metric_name: value}} for the previous run, or None if
        no previous run exists or MLflow is unreachable.
    """
    try:
        import mlflow
        from mlflow.tracking import MlflowClient

        _configure_tracking_uri(config)
        client = MlflowClient()
        experiment = client.get_experiment_by_name(api_name)
        if experiment is None:
            return None

        runs = client.search_runs(
            experiment_ids=[experiment.experiment_id],
            order_by=["start_time DESC"],
            max_results=10,
        )
        previous_run = next((r for r in runs if r.data.tags.get("run_id") != current_run_id), None)
        if previous_run is None:
            return None

        history: dict[int, dict[str, float]] = {}
        for metric in METRIC_NAMES:
            for point in client.get_metric_history(previous_run.info.run_id, metric):
                history.setdefault(point.step, {})[metric] = point.value
        return history
    except Exception as exc:  # noqa: BLE001 - MLflow being unreachable must not crash the pipeline
        logger.warning("Could not fetch previous MLflow run for %s: %s", api_name, exc)
        return None


def detect_regressions(
    current_rows: list[dict[str, Any]],
    previous_metrics: dict[int, dict[str, float]] | None,
    threshold_pct: float,
) -> list[str]:
    """Compare current staircase metrics against the previous run and flag regressions.

    Args:
        current_rows: This run's per-thread-level stat rows.
        previous_metrics: Previous run's {threads: {metric: value}}, or None if unavailable.
        threshold_pct: % degradation in p95 latency or drop in throughput that triggers a flag.

    Returns:
        List of human-readable regression descriptions; empty if none found or no
        previous run is available.
    """
    if not previous_metrics:
        return []

    flags: list[str] = []
    for row in current_rows:
        threads = row["threads"]
        previous = previous_metrics.get(threads)
        if not previous:
            continue

        prev_p95 = previous.get("p95", 0.0)
        if prev_p95 > 0:
            p95_change_pct = (row["p95"] - prev_p95) / prev_p95 * 100
            if p95_change_pct > threshold_pct:
                flags.append(
                    f"p95 at {threads} threads degraded {p95_change_pct:.0f}% vs last run "
                    f"({prev_p95:.0f}ms → {row['p95']:.0f}ms)"
                )

        prev_throughput = previous.get("throughput", 0.0)
        if prev_throughput > 0:
            throughput_change_pct = (prev_throughput - row["throughput"]) / prev_throughput * 100
            if throughput_change_pct > threshold_pct:
                flags.append(
                    f"throughput at {threads} threads dropped {throughput_change_pct:.0f}% vs last run "
                    f"({prev_throughput:.1f}rps → {row['throughput']:.1f}rps)"
                )

    return flags
