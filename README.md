# JMeter Multi-Agent Stress Testing System

A 3-agent sequential pipeline that runs JMeter staircase load tests across a
configurable list of APIs, automatically stops each staircase once an API
saturates, tracks metrics run-over-run to flag regressions, summarises results
with a schema-validated LLM call, and generates a consolidated Word report —
plus a live Grafana view of the run in progress.

```
config.yaml → Agent 1 (runner, early-stop) → Agent 2 (summariser, MLflow + regressions) → Agent 3 (report) → .docx
```

## Requirements

**Native (Mac):**

- Python 3.10+
- JMeter 5.6+ installed and on PATH (auto-detected at `/opt/homebrew/bin/jmeter` on Homebrew installs)
- macOS (Apple Silicon or Intel)

**Containerized (recommended):**

- Docker + Docker Compose

## Install (native)

```bash
pip install -r requirements.txt
```

## Run with Docker (recommended)

The whole pipeline (agents + JMeter) plus the observability stack (MLflow,
Prometheus, Grafana, Loki) run via `docker-compose.yml`. `app` is a one-shot
job, not a daemon — bring the backends up once, then run the pipeline as
needed:

```bash
cp .env.example .env   # fill in API keys if using groq/gemini/anthropic
docker compose up -d mlflow prometheus grafana loki
docker compose run --rm app
```

- Grafana: <http://localhost:3001> (admin/admin) — live staircase view, auto-provisioned
- Prometheus: <http://localhost:9090>
- MLflow: <http://localhost:5000> — per-API run history and metrics

`config.yaml`, `results/`, `final_report/`, and `logs/` are bind-mounted, so
editing `config.yaml` or reading outputs doesn't require rebuilding the image.
Ollama, if used, runs on the Mac host — the `app` container reaches it via
`host.docker.internal`, wired up automatically in `docker-compose.yml`.

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
  breaking_point_error_pct: 5.0 # error % that marks a level as the breaking point (used to early-stop too)
  early_stop_p95_ms: null # optional p95 latency (ms) that also triggers an early stop; null disables
  regression_threshold_pct: 20.0 # % degradation vs the previous MLflow run that gets flagged
  mlflow_tracking_uri: null # null -> MLflow default (./mlruns); overridden by MLFLOW_TRACKING_URI env var
  prometheus_enabled: true # add a Prometheus BackendListener to generated .jmx files
  prometheus_port: 9270 # port JMeter's Prometheus exporter listens on during a run
```

`report_title` is used both as the cover page heading and to derive the final
`.docx` filename — e.g. "API Stress Test Report" produces
`final_report/API_Stress_Test_Report_{date}.docx`.

## Automatic Breaking-Point Detection

Agent 1 no longer always runs the full staircase. After each thread level, it
checks that level's `statistics.json`: if the error rate exceeds
`breaking_point_error_pct`, or (if set) p95 latency exceeds `early_stop_p95_ms`,
it logs `"API {name} saturates at ~{threads} concurrent threads"` and stops —
skipping the remaining, higher thread levels. This directly answers "how many
concurrent users can this API handle" and saves runtime on APIs that saturate early.

## Regression Detection (MLflow)

Agent 2 logs every run's per-thread-level metrics (error %, mean/p95/p99
latency, throughput) to MLflow, one experiment per API, with `step=threads` so
each run's staircase is a proper metric history. Before writing the summary,
it fetches the previous run for that API and flags meaningful degradations —
e.g. `"p95 at 40 threads degraded 32% vs last run (450ms → 594ms)"` — in a
`## Regression vs Previous Run` section that appears in both the `.md` summary
and the final `.docx`, regardless of whether the LLM call succeeds. Browse
full run history at the MLflow UI (`http://localhost:5000` when running via
Docker; a local `./mlruns` directory otherwise).

## Live Metrics (Prometheus + Grafana)

When `settings.prometheus_enabled` is true (default), generated `.jmx` files
include a Backend Listener that exports live metrics via the
[jmeter-prometheus-plugin](https://github.com/johrstrom/jmeter-prometheus-plugin)
on `prometheus_port` (default `9270`) for the duration of each thread level.
The Docker image installs this plugin automatically. Running natively instead,
install it manually into `$JMETER_HOME/lib/ext` (see the Dockerfile for exact
URLs) or set `prometheus_enabled: false` to skip it.

Grafana's starter dashboard (`observability/grafana/dashboards/jmeter-staircase.json`)
includes placeholder panels for latency and error-rate metrics — check
`http://localhost:9270/metrics` during a run and adjust the PromQL queries to
match your installed plugin version's exact metric names.

## Structured Logging

Every agent logs to the console, to a per-run JSON log file at
`logs/{run_id}/{agent}.log`, and (if `LOKI_URL` is set — done automatically by
Docker Compose) pushes the same JSON lines to Loki for the "Pipeline Logs"
panel in Grafana. All log lines from one `python orchestrator.py` execution
share one correlation `run_id`; the exact JMeter command executed is logged
before every run for reproducibility.

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

API keys for paid/hosted providers go in a `.env` file in the project root
(copy `.env.example`; `.env` is never committed).

Both LLM calls (per-API analysis and the executive summary) require the model
to return JSON matching a Pydantic schema (`verdict`/`bottleneck_hypothesis`/
`recommendation`, and `summary`/`most_resilient_api`/`most_fragile_api`/
`overall_recommendation` respectively). If the LLM call fails, times out, or
returns JSON that doesn't validate against the schema, each agent falls back
to a template-based result automatically — the pipeline never crashes and the
report is never corrupted by a malformed LLM response.

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

| File                                                        | Description                                        |
| ------------------------------------------------------------ | --------------------------------------------------- |
| `results/reports/{api_name}/threads_{N}/index.html`         | JMeter HTML dashboard per thread level              |
| `results/reports/{api_name}/threads_{N}/statistics.json`    | Raw numbers read by Agent 2                         |
| `results/summaries/{api_name}.md`                           | Per-API markdown summary (regressions, bottleneck, breaking point) |
| `final_report/{report_title_slug}_{date}.docx`               | Final consolidated Word report                      |
| `logs/{run_id}/{agent}.log`                                  | Per-agent JSON logs for that run                    |
| MLflow experiment `{api_name}`                               | Full metric history across runs                     |

## Troubleshooting

| Issue                                             | Fix                                                                                   |
| --------------------------------------------------- | ---------------------------------------------------------------------------------------- |
| `jmeter: command not found`                       | `export PATH="/opt/homebrew/bin:$PATH"` or set `jmeter_path` in config.yaml              |
| `Too many open files`                             | Agents set `ulimit -n 65536` automatically — retry manually if it still fails            |
| `statistics.json not found`                       | JMeter produced no data — check the API URL and auth token, test with 1 thread first     |
| `Ollama connection refused`                       | Run `ollama serve` in a separate terminal first (native), or check `host.docker.internal` resolves (Docker) |
| No regression section / "no previous run"         | Expected on the first run for a given API — nothing to compare against yet               |
| MLflow unreachable                                | Logged as a warning; summaries still generate, just without run history/regressions      |
| Grafana panels show "No data"                     | The `app` job in Prometheus is only up while a JMeter thread level is actively running    |
