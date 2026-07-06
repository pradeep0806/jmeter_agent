"""Agent 1: generates .jmx test plans from the template and runs JMeter staircase tests."""

import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

if __name__ == "__main__" and __package__ is None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langchain_core.tools import tool

from agents.common import PROJECT_ROOT, get_jmeter_path, load_config, setup_logging

logger = setup_logging(__name__)

RESULTS_DIR: Path = PROJECT_ROOT / "results"
REPORTS_DIR: Path = RESULTS_DIR / "reports"
JTL_PATH: Path = RESULTS_DIR / "r.jtl"


def _build_headers_xml(headers: dict[str, str]) -> str:
    """Render a JMeter HeaderManager collectionProp body from a headers dict.

    Args:
        headers: Mapping of header name to header value.

    Returns:
        XML string of <elementProp> Header entries.
    """
    entries = []
    for name, value in headers.items():
        entries.append(
            "              <elementProp name=\"\" elementType=\"Header\">\n"
            f"                <stringProp name=\"Header.name\">{name}</stringProp>\n"
            f"                <stringProp name=\"Header.value\">{value}</stringProp>\n"
            "              </elementProp>"
        )
    return "\n".join(entries)


def _escape_xml(value: str) -> str:
    """Escape XML-reserved characters in a string.

    Args:
        value: Raw string that will be embedded in XML.

    Returns:
        XML-safe string.
    """
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def generate_jmx(api_config: dict[str, Any], template_path: Path, output_path: Path) -> None:
    """Generate a .jmx file for one API by substituting placeholders in the template.

    Args:
        api_config: One API entry from config.yaml (name, url, method, headers, body).
        template_path: Path to templates/base_template.jmx.
        output_path: Path where the generated .jmx should be written.

    Raises:
        OSError: If the template cannot be read or the output cannot be written.
    """
    parsed = urlparse(api_config["url"])
    protocol = parsed.scheme or "https"
    server_name = parsed.hostname or ""
    port = str(parsed.port or (443 if protocol == "https" else 80))
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"

    headers: dict[str, str] = api_config.get("headers") or {}
    body: str = api_config.get("body") or ""

    with open(template_path, encoding="utf-8") as f:
        jmx_content = f.read()

    replacements = {
        "{{SERVER_NAME}}": _escape_xml(server_name),
        "{{PORT}}": port,
        "{{PROTOCOL}}": protocol,
        "{{PATH}}": _escape_xml(path),
        "{{METHOD}}": api_config.get("method", "GET"),
        "{{BODY}}": _escape_xml(body),
        "{{HEADERS_XML}}": _build_headers_xml(headers),
    }
    for placeholder, value in replacements.items():
        jmx_content = jmx_content.replace(placeholder, value)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(jmx_content)


@tool
def run_jmeter_test(
    jmeter_path: str,
    jmx_path: str,
    jtl_path: str,
    report_dir: str,
    threads: int,
    loops: int,
) -> dict[str, Any]:
    """Run a single JMeter non-GUI test at a given thread level and generate an HTML report.

    Args:
        jmeter_path: Path or command name to the JMeter executable.
        jmx_path: Path to the generated .jmx test plan.
        jtl_path: Path to the temp .jtl results file (deleted before running).
        report_dir: Directory to write the HTML dashboard report to (deleted before running).
        threads: Number of concurrent threads for this staircase level.
        loops: Number of loops per thread.

    Returns:
        A dict with keys "success" (bool), "returncode" (int), "stdout" (str), "stderr" (str).
    """
    jtl_file = Path(jtl_path)
    report_path = Path(report_dir)

    if jtl_file.exists():
        jtl_file.unlink()
    if report_path.exists():
        shutil.rmtree(report_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)

    command = (
        "ulimit -n 65536; "
        'export HEAP="-Xms2g -Xmx4g"; '
        f'"{jmeter_path}" -n '
        f'-t "{jmx_path}" '
        f"-Jthreads={threads} -Jloops={loops} "
        f'-l "{jtl_path}" '
        f'-e -o "{report_dir}"'
    )

    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=3600,
        )
    except subprocess.SubprocessError as exc:
        logger.error("JMeter subprocess failed to launch: %s", exc)
        return {"success": False, "returncode": -1, "stdout": "", "stderr": str(exc)}

    if result.stdout:
        logger.info(result.stdout)
    if result.stderr:
        logger.warning(result.stderr)

    if result.returncode != 0:
        logger.error("JMeter run failed with returncode %s", result.returncode)
        return {
            "success": False,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }

    return {
        "success": True,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def run_staircase_for_api(api_config: dict[str, Any], settings: dict[str, Any]) -> None:
    """Run the full thread-level staircase for one API.

    Generates a .jmx, then runs JMeter once per configured thread level. Individual
    thread-level failures are logged and skipped; they do not abort the staircase.

    Args:
        api_config: One API entry from config.yaml.
        settings: The "settings" block from config.yaml.
    """
    api_name = api_config["name"]
    jmeter_path = get_jmeter_path({"settings": settings})
    template_path = PROJECT_ROOT / settings["jmx_template"]
    jmx_path = PROJECT_ROOT / "results" / f"{api_name}.jmx"

    try:
        generate_jmx(api_config, template_path, jmx_path)
    except OSError as exc:
        logger.error("Could not generate .jmx for %s: %s", api_name, exc)
        return

    thread_levels: list[int] = settings["thread_levels"]
    loops: int = settings["loops_per_level"]

    for threads in thread_levels:
        logger.info("Testing API %s — threads %s", api_name, threads)
        report_dir = REPORTS_DIR / api_name / f"threads_{threads}"

        outcome = run_jmeter_test.invoke(
            {
                "jmeter_path": jmeter_path,
                "jmx_path": str(jmx_path),
                "jtl_path": str(JTL_PATH),
                "report_dir": str(report_dir),
                "threads": threads,
                "loops": loops,
            }
        )

        if not outcome["success"]:
            logger.warning(
                "Skipping thread level %s for %s due to JMeter failure", threads, api_name
            )
            continue


def main() -> None:
    """Entry point: run the staircase test for every API defined in config.yaml."""
    config = load_config()
    settings = config["settings"]
    apis: list[dict[str, Any]] = config["apis"]

    for index, api_config in enumerate(apis, start=1):
        logger.info("=== Testing API %s/%s: %s ===", index, len(apis), api_config["name"])
        try:
            run_staircase_for_api(api_config, settings)
        except Exception as exc:  # noqa: BLE001 - top-level guard so one API never kills the run
            logger.error("Unexpected error testing %s: %s", api_config["name"], exc)

        if index < len(apis):
            interval = settings["api_interval_seconds"]
            logger.info("Waiting %s seconds for backend recovery before next API...", interval)
            time.sleep(interval)


if __name__ == "__main__":
    main()
