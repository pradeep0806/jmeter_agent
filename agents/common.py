"""Shared utilities used by all three agents: config loading, paths, and LLM selection."""

import logging
import os
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
CONFIG_PATH: Path = PROJECT_ROOT / "config.yaml"


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

        return OllamaLLM(model=model)

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
    configured/PATH-resolved value.

    Args:
        config: Parsed config.yaml dictionary.

    Returns:
        Path or command name to invoke JMeter with.
    """
    homebrew_path = Path("/opt/homebrew/bin/jmeter")
    if homebrew_path.exists():
        return str(homebrew_path)
    return config["settings"].get("jmeter_path", "jmeter")


def setup_logging(name: str) -> logging.Logger:
    """Create a module-level logger with a consistent format.

    Args:
        name: Logger name, typically __name__ of the calling module.

    Returns:
        Configured Logger instance.
    """
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger
