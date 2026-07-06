"""Shared utilities used by all three agents: config loading, paths, LLM selection, and logging."""

import contextvars
import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any

import requests
import yaml

PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
CONFIG_PATH: Path = PROJECT_ROOT / "config.yaml"
LOGS_DIR: Path = PROJECT_ROOT / "logs"

_RUN_ID: contextvars.ContextVar[str] = contextvars.ContextVar("run_id", default="")


def get_run_id() -> str:
    """Get the correlation ID for the current pipeline execution, generating one if unset.

    Returns:
        A short run ID shared across all agents invoked from the same orchestrator run.
    """
    run_id = _RUN_ID.get()
    if not run_id:
        run_id = uuid.uuid4().hex[:12]
        _RUN_ID.set(run_id)
    return run_id


def set_run_id(run_id: str) -> None:
    """Set the correlation ID for the current pipeline execution.

    Args:
        run_id: Identifier to stamp on every log line emitted during this run.
    """
    _RUN_ID.set(run_id)


def load_config(config_path: Path = CONFIG_PATH) -> dict[str, Any]:
    """Load and parse config.yaml.

    Args:
        config_path: Path to the YAML config file.

    Returns:
        Parsed config as a dictionary with "settings" and "apis" keys.
    """
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_llm(config: dict[str, Any]) -> Any:
    """Instantiate the configured LangChain LLM based on settings.model_provider.

    Args:
        config: Parsed config.yaml dictionary.

    Returns:
        A LangChain LLM/ChatModel instance.

    Raises:
        ValueError: If model_provider is not one of the supported providers.
    """
    provider: str = config["settings"]["model_provider"]
    model: str = config["settings"]["model_name"]

    if provider == "ollama":
        from langchain_ollama import OllamaLLM

        base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
        return OllamaLLM(model=model, base_url=base_url)

    elif provider == "groq":
        from langchain_groq import ChatGroq

        return ChatGroq(model_name=model, api_key=os.getenv("GROQ_API_KEY"))

    elif provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(model=model, google_api_key=os.getenv("GOOGLE_API_KEY"))

    elif provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(model=model, api_key=os.getenv("ANTHROPIC_API_KEY"))

    else:
        raise ValueError(f"Unsupported model_provider: {provider}")


def get_jmeter_path(config: dict[str, Any]) -> str:
    """Resolve the JMeter executable path.

    Checks the Homebrew default location first, then falls back to the
    configured/PATH-resolved value (the Docker image installs JMeter onto PATH).

    Args:
        config: Parsed config.yaml dictionary.

    Returns:
        Path or command name to invoke JMeter with.
    """
    homebrew_path = Path("/opt/homebrew/bin/jmeter")
    if homebrew_path.exists():
        return str(homebrew_path)
    return config["settings"].get("jmeter_path", "jmeter")


class _RunIdFilter(logging.Filter):
    """Stamps every log record with the current pipeline correlation ID."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = get_run_id()
        return True


class JsonFormatter(logging.Formatter):
    """Formats log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "run_id": getattr(record, "run_id", ""),
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload)


class LokiHandler(logging.Handler):
    """Best-effort log handler that pushes JSON-formatted records to a Loki push API.

    Failures to reach Loki are swallowed so logging never breaks the pipeline.
    """

    def __init__(self, loki_url: str, labels: dict[str, str]):
        super().__init__()
        self.loki_url = loki_url
        self.labels = labels

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
            timestamp_ns = str(int(record.created * 1_000_000_000))
            payload = {
                "streams": [
                    {
                        "stream": {**self.labels, "level": record.levelname.lower()},
                        "values": [[timestamp_ns, line]],
                    }
                ]
            }
            requests.post(self.loki_url, json=payload, timeout=2)
        except Exception:  # noqa: BLE001 - logging must never raise or block the pipeline
            pass


def setup_logging(name: str) -> logging.Logger:
    """Create a module-level logger with console output, a per-run JSON log file, and Loki push.

    Args:
        name: Logger name, typically __name__ of the calling module.

    Returns:
        Configured Logger instance.
    """
    logger = logging.getLogger(name)
    if not logger.handlers:
        run_id_filter = _RunIdFilter()

        console_handler = logging.StreamHandler()
        console_handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s (run=%(run_id)s): %(message)s")
        )
        console_handler.addFilter(run_id_filter)
        logger.addHandler(console_handler)

        run_log_dir = LOGS_DIR / get_run_id()
        run_log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(run_log_dir / f"{name.rsplit('.', 1)[-1]}.log", encoding="utf-8")
        file_handler.setFormatter(JsonFormatter())
        file_handler.addFilter(run_id_filter)
        logger.addHandler(file_handler)

        loki_url = os.getenv("LOKI_URL")
        if loki_url:
            loki_handler = LokiHandler(loki_url, labels={"job": "jmeter_agent", "logger": name})
            loki_handler.setFormatter(JsonFormatter())
            loki_handler.addFilter(run_id_filter)
            logger.addHandler(loki_handler)

        logger.setLevel(logging.INFO)
    return logger


def read_thread_level_stats(reports_dir: Path, api_name: str, threads: int) -> dict[str, Any] | None:
    """Read and extract metrics from statistics.json for one API at one thread level.

    Shared by runner_agent (early-stop checks) and summariser_agent (full analysis)
    so the JMeter statistics.json parsing logic exists in exactly one place.

    Args:
        reports_dir: Root reports directory (results/reports).
        api_name: Name of the API as defined in config.yaml.
        threads: Thread count for this staircase level.

    Returns:
        Dict with threads, sampleCount, meanResTime, minResTime, maxResTime, errorPct,
        p95, p99, throughput — or None if statistics.json is missing/unparseable.
    """
    stats_path = reports_dir / api_name / f"threads_{threads}" / "statistics.json"
    if not stats_path.exists():
        logging.getLogger(__name__).warning(
            "statistics.json not found for %s at threads=%s, skipping", api_name, threads
        )
        return None

    try:
        with open(stats_path, encoding="utf-8") as f:
            stats = json.load(f)
        total = stats["Total"]
    except (json.JSONDecodeError, KeyError, OSError) as exc:
        logging.getLogger(__name__).warning(
            "Could not parse statistics.json for %s at threads=%s: %s", api_name, threads, exc
        )
        return None

    return {
        "threads": threads,
        "sampleCount": total.get("sampleCount", 0),
        "meanResTime": total.get("meanResTime", 0.0),
        "minResTime": total.get("minResTime", 0.0),
        "maxResTime": total.get("maxResTime", 0.0),
        "errorPct": total.get("errorPct", 0.0),
        "p95": total.get("pct2ResTime", 0.0),
        "p99": total.get("pct3ResTime", 0.0),
        "throughput": total.get("throughput", 0.0),
    }
