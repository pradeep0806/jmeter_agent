"""Agent 3: reads all per-API markdown summaries and writes the final consolidated .docx report."""

import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

if __name__ == "__main__" and __package__ is None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt, RGBColor

from agents.common import PROJECT_ROOT, get_llm, setup_logging

logger = setup_logging(__name__)

SUMMARIES_DIR: Path = PROJECT_ROOT / "results" / "summaries"
FINAL_REPORT_DIR: Path = PROJECT_ROOT / "final_report"

COLOR_HEALTHY = RGBColor(0x00, 0xB0, 0x50)
COLOR_DEGRADING = RGBColor(0xFF, 0x99, 0x00)
COLOR_CRITICAL = RGBColor(0xFF, 0x00, 0x00)

BODY_FONT = "Calibri"
BODY_SIZE = Pt(11)
HEADING_SIZE = Pt(14)

EXEC_SUMMARY_PROMPT = """You are a performance engineering lead writing an executive summary \
for a stress test report covering {count} APIs.

Per-API summary (name, safe limit, breaking point, max error %):
{overview}

Write a short executive summary (2-3 paragraphs, plain prose, no markdown headings) that:
1. Compares overall resilience across the APIs.
2. Identifies which API is the most resilient and which is the most fragile.
3. Gives an overall system recommendation.
"""


def _status_color(status: str) -> RGBColor:
    """Map a status label (as emitted by the summariser agent) to a docx font color.

    Args:
        status: Status string, e.g. "✅ Healthy", "⚠️ Degrading", "💀 Critical".

    Returns:
        RGBColor for the matching health tier; defaults to critical red if unrecognised.
    """
    if "Healthy" in status:
        return COLOR_HEALTHY
    if "Degrading" in status:
        return COLOR_DEGRADING
    return COLOR_CRITICAL


def parse_summary_md(md_text: str) -> dict[str, Any]:
    """Parse a markdown summary file produced by summariser_agent into structured data.

    Args:
        md_text: Full contents of a results/summaries/{api_name}.md file.

    Returns:
        Dict with name, url, method, test_date, rows, key_findings, safe_limit,
        breaking_point, breaking_pct, and recommendation.
    """
    name_match = re.search(r"^# API Stress Test Summary: (.+)$", md_text, re.MULTILINE)
    url_match = re.search(r"\*\*URL:\*\* (.+)", md_text)
    method_match = re.search(r"\*\*Method:\*\* (.+)", md_text)
    date_match = re.search(r"\*\*Test Date:\*\* (.+)", md_text)
    safe_match = re.search(r"\*\*Safe limit:\*\* (\S+)", md_text)
    breaking_match = re.search(
        r"\*\*Breaking point:\*\* (\S+) concurrent users \(([^)]+)\)", md_text
    )
    findings_match = re.search(
        r"## Key Findings\n\n(.+?)\n\n## Breaking Point", md_text, re.DOTALL
    )
    recommendation_match = re.search(
        r"## Recommendation\n\n(.+?)\s*$", md_text, re.DOTALL
    )

    rows: list[dict[str, str]] = []
    for line in md_text.splitlines():
        if (
            line.startswith("|")
            and not line.startswith("| -")
            and "Threads" not in line
        ):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) == 7 and cells[0].isdigit():
                rows.append(
                    {
                        "threads": cells[0],
                        "requests": cells[1],
                        "avg": cells[2],
                        "min": cells[3],
                        "max": cells[4],
                        "error_pct": cells[5],
                        "status": cells[6],
                    }
                )

    return {
        "name": name_match.group(1).strip() if name_match else "unknown",
        "url": url_match.group(1).strip() if url_match else "",
        "method": method_match.group(1).strip() if method_match else "",
        "test_date": date_match.group(1).strip() if date_match else "",
        "rows": rows,
        "key_findings": findings_match.group(1).strip() if findings_match else "",
        "safe_limit": safe_match.group(1).strip() if safe_match else "N/A",
        "breaking_point": (
            breaking_match.group(1).strip() if breaking_match else "Not reached"
        ),
        "breaking_pct": breaking_match.group(2).strip() if breaking_match else "N/A",
        "recommendation": (
            recommendation_match.group(1).strip() if recommendation_match else ""
        ),
    }


def load_all_summaries() -> list[dict[str, Any]]:
    """Load and parse every markdown summary file in results/summaries/.

    Returns:
        List of parsed summary dicts, one per API, in filename-sorted order.
    """
    summaries = []
    if not SUMMARIES_DIR.exists():
        return summaries
    for md_path in sorted(SUMMARIES_DIR.glob("*.md")):
        try:
            with open(md_path, encoding="utf-8") as f:
                summaries.append(parse_summary_md(f.read()))
        except OSError as exc:
            logger.warning("Could not read summary %s: %s", md_path, exc)
    return summaries


def _max_error_pct(summary: dict[str, Any]) -> float:
    """Compute the maximum error % observed across a summary's rows.

    Args:
        summary: Parsed summary dict as returned by parse_summary_md.

    Returns:
        Maximum error percentage as a float, or 0.0 if no rows are present.
    """
    values = []
    for row in summary["rows"]:
        try:
            values.append(float(row["error_pct"].rstrip("%")))
        except ValueError:
            continue
    return max(values) if values else 0.0


def report_title_slug(config: dict[str, Any]) -> str:
    """Derive a filesystem-safe slug from settings.report_title for use in output filenames.

    Args:
        config: Parsed config.yaml dictionary.

    Returns:
        The report title with non-alphanumeric runs collapsed to underscores.
    """
    title = config["settings"].get("report_title", "API Stress Test Report")
    return re.sub(r"[^A-Za-z0-9]+", "_", title).strip("_")


def build_report_filename(config: dict[str, Any]) -> str:
    """Build the dated output filename for the final report from settings.report_title.

    Args:
        config: Parsed config.yaml dictionary.

    Returns:
        Filename in the form "{Report_Title_Slug}_{date}.docx".
    """
    return f"{report_title_slug(config)}_{date.today().isoformat()}.docx"


def _overall_status(max_error_pct: float) -> str:
    """Map a max error % to an overall status label using CLAUDE.md thresholds.

    Args:
        max_error_pct: Maximum error percentage observed for an API.

    Returns:
        One of "Healthy", "Degrading", "Critical".
    """
    if max_error_pct < 1.0:
        return "Healthy"
    if max_error_pct <= 10.0:
        return "Degrading"
    return "Critical"


def generate_executive_summary(
    config: dict[str, Any], summaries: list[dict[str, Any]]
) -> str:
    """Ask the configured LLM for an executive summary, falling back to a template on failure.

    Args:
        config: Parsed config.yaml dictionary.
        summaries: List of parsed per-API summary dicts.

    Returns:
        Executive summary text (plain prose).
    """
    overview_lines = []
    for s in summaries:
        overview_lines.append(
            f"- {s['name']}: safe limit={s['safe_limit']}, breaking point={s['breaking_point']}, "
            f"max error={_max_error_pct(s):.2f}%"
        )
    overview = "\n".join(overview_lines)

    try:
        llm = get_llm(config)
        prompt = EXEC_SUMMARY_PROMPT.format(count=len(summaries), overview=overview)
        response = llm.invoke(prompt)
        text = response.content if hasattr(response, "content") else str(response)
        if text.strip():
            return text.strip()
        logger.warning(
            "LLM returned an empty executive summary, using template fallback"
        )
    except (
        Exception
    ) as exc:  # noqa: BLE001 - any LLM failure must fall back, never crash
        logger.warning(
            "Executive summary LLM call failed: %s — using template fallback", exc
        )

    ranked = sorted(summaries, key=_max_error_pct)
    most_resilient = ranked[0]["name"] if ranked else "N/A"
    most_fragile = ranked[-1]["name"] if ranked else "N/A"
    return (
        f"This report covers {len(summaries)} APIs tested under staircase load. "
        f"{most_resilient} showed the strongest resilience under load, while {most_fragile} "
        f"degraded earliest. Overall, production concurrency should be capped at each API's "
        f"individually determined safe limit, with the most fragile services prioritised for "
        f"remediation."
    )


def _set_body_style(document: Document) -> None:
    """Set the default document font to Calibri 11pt per CLAUDE.md styling rules.

    Args:
        document: The python-docx Document to style.
    """
    style = document.styles["Normal"]
    style.font.name = BODY_FONT
    style.font.size = BODY_SIZE


def _add_heading(document: Document, text: str, level: int) -> None:
    """Add a heading paragraph styled with the Calibri font and 14pt size.

    Args:
        document: The python-docx Document to append to.
        text: Heading text.
        level: Heading level (1 for section titles, 2 for subsections).
    """
    heading = document.add_heading(text, level=level)
    for run in heading.runs:
        run.font.name = BODY_FONT
        run.font.size = HEADING_SIZE


def _add_results_table(document: Document, rows: list[dict[str, str]]) -> None:
    """Add a color-coded results table for one API's staircase results.

    Args:
        document: The python-docx Document to append to.
        rows: Parsed table rows (threads, requests, avg, min, max, error_pct, status).
    """
    table = document.add_table(rows=1, cols=7)
    table.style = "Light Grid Accent 1"
    headers = [
        "Threads",
        "Requests",
        "Avg (ms)",
        "Min (ms)",
        "Max (ms)",
        "Error %",
        "Status",
    ]
    for cell, header in zip(table.rows[0].cells, headers):
        cell.text = header

    for row in rows:
        cells = table.add_row().cells
        values = [
            row["threads"],
            row["requests"],
            row["avg"],
            row["min"],
            row["max"],
            row["error_pct"],
            row["status"],
        ]
        color = _status_color(row["status"])
        for cell, value in zip(cells, values):
            cell.text = value
            for paragraph in cell.paragraphs:
                for run in paragraph.runs:
                    run.font.color.rgb = color


def _add_footer(document: Document) -> None:
    """Add the standard footer text to every section of the document.

    Args:
        document: The python-docx Document to add a footer to.
    """
    for section in document.sections:
        footer = section.footer
        paragraph = (
            footer.paragraphs[0] if footer.paragraphs else footer.add_paragraph()
        )
        paragraph.text = "Generated by JMeter Agent"
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        for run in paragraph.runs:
            run.font.name = BODY_FONT
            run.font.size = Pt(9)


def build_report(config: dict[str, Any], summaries: list[dict[str, Any]]) -> Document:
    """Assemble the full consolidated .docx report document.

    Args:
        config: Parsed config.yaml dictionary.
        summaries: List of parsed per-API summary dicts.

    Returns:
        The assembled python-docx Document, ready to be saved.
    """
    document = Document()
    _set_body_style(document)

    # Cover page
    report_title = config["settings"].get("report_title", "API Stress Test Report")
    title = document.add_heading(report_title, level=0)
    for run in title.runs:
        run.font.name = BODY_FONT
    subtitle = document.add_paragraph(f"Test Date: {date.today().isoformat()}")
    subtitle.add_run("\nEnvironment: Mac (Apple Silicon), JMeter 5.6.3")
    document.add_page_break()

    # Table of contents
    _add_heading(document, "Table of Contents", level=1)
    document.add_paragraph("1. Executive Summary")
    for i, s in enumerate(summaries, start=2):
        document.add_paragraph(f"{i}. {s['name']}")
    document.add_paragraph(f"{len(summaries) + 2}. Overall System Health Summary")
    document.add_page_break()

    # Executive summary
    _add_heading(document, "Executive Summary", level=1)
    document.add_paragraph(generate_executive_summary(config, summaries))
    document.add_page_break()

    # Per-API sections
    for s in summaries:
        _add_heading(document, s["name"], level=1)
        document.add_paragraph(f"URL: {s['url']}")
        document.add_paragraph(f"Method: {s['method']}")
        document.add_paragraph(f"Test Date: {s['test_date']}")

        _add_heading(document, "Results", level=2)
        _add_results_table(document, s["rows"])

        _add_heading(document, "Key Findings", level=2)
        document.add_paragraph(s["key_findings"])

        _add_heading(document, "Breaking Point", level=2)
        document.add_paragraph(f"Safe limit: {s['safe_limit']} concurrent users")
        document.add_paragraph(
            f"Breaking point: {s['breaking_point']} concurrent users ({s['breaking_pct']} error rate)"
        )

        _add_heading(document, "Recommendation", level=2)
        document.add_paragraph(s["recommendation"])
        document.add_page_break()

    # Overall system health summary
    _add_heading(document, "Overall System Health Summary", level=1)
    table = document.add_table(rows=1, cols=5)
    table.style = "Light Grid Accent 1"
    for cell, header in zip(
        table.rows[0].cells,
        ["API Name", "Safe Limit", "Breaking Point", "Max Error %", "Status"],
    ):
        cell.text = header
    for s in summaries:
        max_pct = _max_error_pct(s)
        status = _overall_status(max_pct)
        cells = table.add_row().cells
        for cell, value in zip(
            cells,
            [
                s["name"],
                s["safe_limit"],
                s["breaking_point"],
                f"{max_pct:.2f}%",
                status,
            ],
        ):
            cell.text = value

    _add_footer(document)
    return document


def main() -> None:
    """Entry point: read all summaries and write the final consolidated .docx report."""
    from agents.common import load_config

    config = load_config()
    summaries = load_all_summaries()

    if not summaries:
        logger.warning(
            "No summaries found in %s, skipping report generation", SUMMARIES_DIR
        )
        return

    document = build_report(config, summaries)

    FINAL_REPORT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = FINAL_REPORT_DIR / build_report_filename(config)
    document.save(str(output_path))
    logger.info("Final report saved to %s", output_path)


if __name__ == "__main__":
    main()
