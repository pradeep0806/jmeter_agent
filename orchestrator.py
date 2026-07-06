"""Entry point for the JMeter multi-agent stress testing pipeline.

Runs the three agents sequentially: runner_agent -> summariser_agent -> report_agent.
Usage: python orchestrator.py
"""

from agents import report_agent, runner_agent, summariser_agent
from agents.common import load_config, setup_logging

logger = setup_logging(__name__)


def main() -> None:
    """Run all three agents sequentially and print the path to the final report."""
    print("=== Stage 1/3: Running JMeter tests ===")
    runner_agent.main()

    print("=== Stage 2/3: Summarising results ===")
    summariser_agent.main()

    print("=== Stage 3/3: Generating final report ===")
    report_agent.main()

    config = load_config()
    slug = report_agent.report_title_slug(config)
    reports = sorted(report_agent.FINAL_REPORT_DIR.glob(f"{slug}_*.docx"))
    if reports:
        print(f"Final report: {reports[-1]}")
    else:
        print("No final report was generated — check logs above for errors.")


if __name__ == "__main__":
    main()
