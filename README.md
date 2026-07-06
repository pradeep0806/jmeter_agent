# JMeter Multi-Agent Stress Testing System

A local, 3-agent sequential pipeline that runs JMeter staircase load tests across a
configurable list of APIs, summarises the results with an LLM, and generates a
consolidated Word report — all running on Mac.

```
config.yaml → Agent 1 (runner) → Agent 2 (summariser) → Agent 3 (report) → .docx
```

## Requirements

- Python 3.10+
- JMeter 5.6+ installed and on PATH (auto-detected at `/opt/homebrew/bin/jmeter` on Homebrew installs)
- macOS (Apple Silicon or Intel)

## Install

```bash
pip install -r requirements.txt
```

## Configure APIs (config.yaml)

`config.yaml` is the only file you need to edit to add, remove, or change APIs.

To **add** an API, append a block under `apis:`:

```yaml
apis:
  - name: my_new_api
    url: https://your-host.com/api/new-endpoint
    method: POST
    headers:
      Content-Type: application/json
      Authorization: Bearer YOUR_TOKEN
    body: |
      {"param": "value"}
```

To **remove** an API, delete its block.

Global test settings also live in `config.yaml`:

```yaml
settings:
  thread_levels: [10, 15, 20, 25, 30, 35, 40, 45, 50] # staircase levels
  loops_per_level: 20
  api_interval_seconds: 30 # recovery time between APIs
  report_title: API Stress Test Report # cover page title; also derives the output filename
```

`report_title` is used both as the cover page heading and to derive the final
`.docx` filename — e.g. "API Stress Test Report" produces
`final_report/API_Stress_Test_Report_{date}.docx`.

## Switch LLM Provider

Set `settings.model_provider` and `settings.model_name` in `config.yaml`:

| Provider    | model_name examples                    | Cost         | Requires                                    |
| ----------- | -------------------------------------- | ------------ | ------------------------------------------- |
| `ollama`    | `llama3`, `mistral`, `phi3`            | Free (local) | `brew install ollama && ollama pull llama3` |
| `groq`      | `llama3-8b-8192`, `mixtral-8x7b-32768` | Free tier    | `GROQ_API_KEY` env var                      |
| `gemini`    | `gemini-pro`, `gemini-1.5-flash`       | Free tier    | `GOOGLE_API_KEY` env var                    |
| `anthropic` | `claude-3-haiku-20240307`              | Paid         | `ANTHROPIC_API_KEY` env var                 |

Recommended for local/zero-cost use: `ollama` with `llama3`.

```bash
brew install ollama
ollama pull llama3
ollama serve   # run in a separate terminal
```

API keys for paid/hosted providers go in a `.env` file in the project root (never committed):

```
GROQ_API_KEY=your_key
GOOGLE_API_KEY=your_key
ANTHROPIC_API_KEY=your_key
```

If the LLM call fails for any reason, each agent falls back to a template-based
summary automatically — the pipeline never crashes because of the LLM.

## Run

```bash
python orchestrator.py
```

This runs all three agents sequentially and prints the path to the final `.docx`
report at the end.

To debug a single stage:

```bash
python agents/runner_agent.py
python agents/summariser_agent.py
python agents/report_agent.py
```

## Output

| File                                                     | Description                            |
| -------------------------------------------------------- | -------------------------------------- |
| `results/reports/{api_name}/threads_{N}/index.html`      | JMeter HTML dashboard per thread level |
| `results/reports/{api_name}/threads_{N}/statistics.json` | Raw numbers read by Agent 2            |
| `results/summaries/{api_name}.md`                        | Per-API markdown summary               |
| `final_report/{report_title_slug}_{date}.docx`           | Final consolidated Word report (name derived from `settings.report_title`) |

## Troubleshooting

| Issue                       | Fix                                                                                  |
| --------------------------- | ------------------------------------------------------------------------------------ |
| `jmeter: command not found` | `export PATH="/opt/homebrew/bin:$PATH"` or set `jmeter_path` in config.yaml          |
| `Too many open files`       | Agents set `ulimit -n 65536` automatically — retry manually if it still fails        |
| `statistics.json not found` | JMeter produced no data — check the API URL and auth token, test with 1 thread first |
| `Ollama connection refused` | Run `ollama serve` in a separate terminal first                                      |
