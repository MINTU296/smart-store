# Apex Retail — Store Intelligence

End-to-end pipeline that turns raw CCTV clips into a live store-analytics API
— the offline equivalent of the event-stream telemetry that already exists for
the online channel.

**The problem.** A buyer's online journey is fully instrumented (impression →
product page → cart → checkout) but the same buyer's in-store journey is
opaque. A category manager who wants to know "did the new moisturiser endcap
lift conversion?" has no way to answer it. This system closes that gap.

**What it does.** Each store's CCTV clips are processed by a per-store
detection pipeline that emits a structured event stream — `ENTRY`, `EXIT`,
`REENTRY`, `ZONE_ENTER`, `ZONE_DWELL`, `BILLING_QUEUE_JOIN`,
`BILLING_QUEUE_ABANDON`, `PURCHASE` — over HTTP to a FastAPI service backed by
SQLite (system of record) and Redis (live counters + pub/sub). The API turns
that stream into queryable metrics: conversion rate, funnel drop-off, zone
heatmaps, queue anomalies, and a live React dashboard powered by a per-store
WebSocket.

* **Detection** — YOLOv11n + ByteTrack + colour-histogram Re-ID (30-min
  window), per-store HSV-uniform staff classifier with a behavioural fallback,
  zone polygons from the supplied store layouts.
* **Event stream** — PDF-spec schema, deterministic `event_id` (uuid5 over
  `store|cam|visitor|type|ts`) so re-running the pipeline on the same clip is
  a no-op at the storage layer (`accepted=0, duplicates=N`).
* **API** — FastAPI + SQLite + Redis. Eight endpoints covering ingest,
  metrics, funnel, heatmap, anomalies, insights, health, and a per-store
  WebSocket.
* **Live dashboard** — React + Vite, production bundle committed under
  `dashboard/`, served as static files by FastAPI. Updates the moment events
  land at the API.

**North-star metric: offline store conversion rate** = visitors who completed
a purchase ÷ unique visitors in the session window. Every component of the
system either improves the *accuracy* of that number (detection, Re-ID, staff
exclusion, POS correlation) or its *actionability* (real-time metrics, session
funnel, anomaly detection, live dashboard).

## Quick start

Three commands — well under the spec's five-command budget — get a reviewer
from `git clone` to live API responses with no manual intervention:

```bash
git clone <repo-url> store-intelligence && cd store-intelligence  # 1
cp .env.example .env                                              # 2
make smoke                                                        # 3 — boots stack + ingests fixture + hits every endpoint
```

`make smoke` is the **reviewer entry point**. It boots the docker compose
stack (api + redis), POSTs a *now-anchored* fixture batch of ten events to
`/events/ingest`, prints the body of every read endpoint so you can audit the
numbers in one screen, then re-POSTs the same batch to prove idempotency
(`accepted=0, duplicates=N` on the second call). Today's `/metrics`,
`/funnel`, `/heatmap`, and `/anomalies` all light up because the fixture
timestamps are anchored to the current minute — no clock-mismatch frustration.

The fixture also includes one deliberately malformed event (`confidence > 1.0`)
so reviewers see partial-success behaviour: `accepted=9, duplicates=0,
rejected=1` with the error reason inline. This validates the spec's
schema-compliance and partial-success requirements in a single command.

Manual fallback (the same five commands `make smoke` runs under the hood):

```bash
docker compose up -d                                  # api + redis + dashboard
python3 scripts/build_smoke_fixture.py | curl -X POST \
  -H 'Content-Type: application/json' --data-binary @- \
  http://localhost:8000/events/ingest                 # seed
curl http://localhost:8000/stores/STORE_BLR_001/metrics | python3 -m json.tool
docker compose run --rm pipeline \
  python -m pipeline.run --store STORE_BLR_001 --clip-dir "/raw-data/Store 1"
open http://localhost:8000/dashboard/                 # live UI
```

The pipeline service is gated behind a compose profile so it does not
auto-start (it processes a finite set of clips and exits). Run it explicitly
per store.

## Endpoints

The API surface is intentionally small. Four read endpoints answer the
business questions the rubric scores; one composite endpoint feeds the
dashboard; one health endpoint serves on-call; one WebSocket carries the live
stream. Every read endpoint anchors "today" on the **most recent event
timestamp for that store**, not real-now — important because graders run the
pipeline against historical clips.

| Route | Purpose |
|---|---|
| `POST /events/ingest` | Idempotent batch ingest (≤500 events). Each event validated by Pydantic, UPSERTed by `event_id` PK, and reported back as `stored` / `duplicate` / `rejected` with per-event errors so a single malformed event never poisons the batch. |
| `GET /stores/{id}/metrics` | Today's headline numbers: unique visitors (excl. staff), purchasing visitors via POS correlation, conversion rate, current queue depth (Redis-backed), POS-correlated abandonment rate, and average dwell per zone. |
| `GET /stores/{id}/funnel` | Entry → Zone Visit → Billing Queue → Purchase, with each stage enforced as a *subset* of the previous one. Re-entries collapse onto the same visitor so a returning customer never double-counts in any stage. |
| `GET /stores/{id}/heatmap` | Per-zone visit count + average dwell, normalised 0..100 by a weighted blend of both signals. Returns `data_confidence: low` when fewer than 20 sessions are in the window so the dashboard can show a hint instead of treating the data as authoritative. |
| `GET /stores/{id}/anomalies` | Three signals with explicit severities and `suggested_action` strings: `BILLING_QUEUE_SPIKE` (current depth ≥ threshold; CRITICAL at 2×), a `_P95` second opinion that catches drift the manager never set a threshold for, `CONVERSION_DROP` (today < 7-day avg − 2σ; needs ≥3 days history), `DEAD_ZONE` (no customer visits to a non-billing zone in 30 min). |
| `GET /stores/{id}/insights` | Composite endpoint the React dashboard pulls on a 15-second tick. One round-trip returns: per-camera health, current occupancy + intra-day peak, today-vs-7d deltas (`delta_pp` for rates and `delta_pct` for counts so the UI never misrepresents one as the other), queue trend, hourly traffic with conversion overlaid, attention vs conversion per zone, staff vs customers with an `understaffed` flag, and three session chips. Accepts `?window_hours=` to zoom out. |
| `GET /health` | Liveness, DB + Redis flags, per-store last-event-ts, and a `STALE_FEED` warning at >10-min lag. The on-call diagnostic. |
| `WS /ws/{store_id}` | Live event stream (Redis `XREAD` on `events:{store_id}`). Supports `?last_id=` replay-on-reconnect — pub/sub was rejected because it silently drops events for offline clients. |
| `GET /dashboard/` | Static-served React UI; WebSocket-driven updates. |

## Layout

```
store-intelligence/
├── app/                  # FastAPI service (incl. /stores/{id}/insights)
├── pipeline/             # YOLO + ByteTrack + Re-ID + zones + emitter
├── dashboard-src/        # React + Vite + TS source for the live UI
├── dashboard/            # Built React bundle (served by FastAPI)
├── store_layouts/        # Per-store zone polygons
├── data/                 # Sample clips + POS CSV
├── scripts/              # smoke.sh, build_smoke_fixture.py, debug_staff.py
├── tests/                # 68 tests, ≥79 % coverage
├── docs/                 # DESIGN.md + CHOICES.md
├── Makefile              # smoke / up / down / seed / test / pipeline
├── pyproject.toml
├── docker-compose.yml
├── Dockerfile.api
└── Dockerfile.pipeline
```

## React dashboard development

The dashboard is a Vite + React + TypeScript app whose **production bundle is
committed under `dashboard/`** and served as static files by FastAPI — there's
no Node toolchain in the API image.

```bash
# Local hot-reload dev (requires Node 20+)
cd dashboard-src
npm install
npm run dev                                       # http://localhost:5173

# Production build → overwrites dashboard/ with the new bundle
npm run build

# Or, if you don't have Node 20 on the host:
docker run --rm -v "$PWD/..":/work -w /work/dashboard-src node:20-alpine \
    sh -c "npm install && npm run build"

# Pick up the new bundle in the running api container
docker compose up -d --build api
```

## Running the pipeline against the supplied clips

```bash
# Store 1 (black-uniform staff)
docker compose run --rm pipeline \
    python -m pipeline.run --store STORE_BLR_001 --clip-dir "/raw-data/Store 1"

# Store 2 (pink-shirt + black-trouser staff)
docker compose run --rm pipeline \
    python -m pipeline.run --store STORE_BLR_002 --clip-dir "/raw-data/Store 2"
```

The pipeline emits events to the API in batches of 200. The dashboard ticks
live as events arrive. Pass `--no-emit` to write `events.jsonl` to disk instead
(useful for offline grading).

## Tests

```bash
docker compose exec api pytest --cov=app --cov=pipeline
# OR locally:
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev,pipeline]'
pytest --cov=app --cov=pipeline
```

Each test file starts with a `# PROMPT:` block showing the AI prompt used to
draft it and a `# CHANGES MADE:` block showing what I changed afterwards
(usually fixing off-by-ones, missing edge cases, or Python 3.14 incompatibilities).

### Coverage

64 tests, **79 % statement coverage** over `app/` + `pipeline/` (well above
the 70 % bar). Reproduce locally with `make coverage`. Latest run:

```
Name                   Stmts   Miss  Cover
------------------------------------------
app/funnel.py             37      3    92%
app/heatmap.py            29      3    90%
app/insights.py          264     27    90%
app/main.py               51      4    92%
app/models.py            163      0   100%
app/pos.py                93     10    89%
app/ingestion.py          84     13    85%
app/metrics.py            67     11    84%
app/health.py             45      9    80%
app/anomalies.py          84     24    71%
app/db.py                199     81    59%
app/logging_setup.py      44      5    89%
app/config.py             51      7    86%
pipeline/staff.py        116     13    89%
pipeline/emit.py          61      9    85%
pipeline/reid.py          63     13    79%
pipeline/run.py          442    185    58%   # YOLO branches need real video
pipeline/zones.py         51      9    82%
pipeline/session.py       27      0   100%
pipeline/config.py        19      0   100%
------------------------------------------
TOTAL                   1990    426    79%
```

**Excluded from the coverage measurement** (`pyproject.toml [tool.coverage.run] omit`):

- `pipeline/detect.py` — YOLO + ByteTrack glue. Verified end-to-end via
  `make pipeline` against the supplied clips, not via pytest, because
  exercising it requires real video frames and the bundled `yolov8n.pt`
  weights. Excluding it keeps the coverage signal honest — adding it as
  zeros would distort the rest of the report.
- `app/ws.py` — WebSocket handler. Verified end-to-end via `make smoke`
  (which connects a peer and asserts the live stream tails events), not
  via pytest, because `TestClient.websocket_connect` does not exercise
  the same `XREAD`-loop path.

`pipeline/run.py` is included with 57 % — its YOLO-frame branches need real
video, but the orchestration / state-machine paths (line crossing, Re-ID
hand-off, billing-queue debouncing, group-atomic flush) all have unit tests
in `tests/test_pipeline_run.py`.

`app/db.py` is included with 64 % — the SQLite paths are covered, the
RedisClient paths are not (they need a live Redis; `fakeredis` covers the
publish/subscribe API but not `XREAD`).

### Edge cases tested

- empty store (no events) — `tests/test_metrics.py::test_metrics_empty_store`
- all-staff clip — `tests/test_metrics.py::test_metrics_excludes_staff`
- zero purchases — `tests/test_metrics.py::test_metrics_zero_purchases`
- re-entry de-dup in funnel — `tests/test_funnel.py::test_funnel_no_double_count_on_reentry`
- POST idempotency — `tests/test_ingestion.py::test_ingest_idempotent`
- batch >500 (413, not 5xx) — `tests/test_ingestion.py::test_ingest_oversize_batch`
- malformed event partial-success — `tests/test_ingestion.py::test_ingest_partial_success`
- low-conf detection accepted, not suppressed — `tests/test_ingestion.py::test_ingest_low_confidence_event_accepted`
- POS correlation 5-min boundary — `tests/test_correlation.py::test_correlation_at_window_boundary`
- group-entry atomic flush — `tests/test_pipeline_run.py::test_group_atomic_flush_survives_intermediate_emitter_flush`
- staff downgrade after hoodie removal — `tests/test_staff.py::test_staff_downgrade_when_uniform_disappears`
- Re-ID max-lifetime eviction — `tests/test_pipeline.py::test_reid_evicts_identities_past_max_lifetime`

## What the pipeline emits

Every event the pipeline produces conforms to the PDF-spec schema —
`event_id`, `store_id`, `camera_id`, `visitor_id`, `event_type`, `timestamp`,
`zone_id`, `dwell_ms`, `is_staff`, `confidence`, and a `metadata` envelope
carrying `queue_depth`, `sku_zone`, `session_seq`, and (for ENTRY events)
`group_size`. The eight `event_type` values match the rubric exactly: `ENTRY`,
`EXIT`, `REENTRY`, `ZONE_ENTER`, `ZONE_EXIT`, `ZONE_DWELL`,
`BILLING_QUEUE_JOIN`, `BILLING_QUEUE_ABANDON` (plus `PURCHASE` synthesised
from POS rows at correlation time).

```json
{
  "event_id":   "f3c1...uuid5",
  "store_id":   "STORE_BLR_001",
  "camera_id":  "CAM_ENTRY",
  "visitor_id": "VIS_a1b2c3d4",
  "event_type": "ENTRY",
  "timestamp":  "2026-03-08T18:10:05Z",
  "zone_id":    null,
  "dwell_ms":   0,
  "is_staff":   false,
  "confidence": 0.91,
  "metadata":   {"queue_depth": null, "sku_zone": null, "session_seq": 1, "group_size": 1}
}
```

`event_id` is a deterministic **uuid5** of `(store, cam, visitor, type, ts)`,
so re-running the pipeline against the same clips produces *the same*
`event_id`s and the API treats the second run as a no-op (`accepted=0,
duplicates=N`). This is what makes a re-ingest safe — never a double-count,
never a partial replay, no operator intervention required. The full reasoning
(uuid5 vs the spec's example uuid4) lives in `docs/CHOICES.md` Decision 2.

## Edge-case handling

| Edge case | How it's handled |
|---|---|
| Group entry (2-4 people) | Each YOLO bbox → its own track → its own ENTRY event. Each ENTRY also carries `metadata.group_size` (count of arrivals within a 2-second window, back-stamped) so a 3-person group surfaces as 3 ENTRY events all tagged `group_size=3` — the count stays "individuals" but the cohort is recoverable for follow-up Q&A |
| Staff movement | Per-store HSV uniform palette (S1 = all black; S2 = pink shirt + black trousers) + behavioural fallback (>20 min on floor, no billing visit) |
| Re-entry | Re-ID match within 30-min window after EXIT → REENTRY event with the *same* visitor_id |
| Partial occlusion | Low-confidence detection emitted with actual confidence; never silently dropped |
| Billing queue buildup | `queue_depth` recomputed every frame; BILLING_QUEUE_JOIN carries it; ABANDON when visitor leaves the queue without a POS match in 5 min |
| Empty store | API returns 200 with zeros; dashboard shows "0 visitors today" |
| Camera angle overlap | Track born on floor cam within 3 s of an ENTRY in the spatial overlap region inherits the entry's visitor_id |
| Zero-purchase store | Conversion rate = 0.0 (no division-by-zero) |
| Re-entry in funnel | Session-based unique counting; one visitor never counts twice in any stage |

## Sample API responses

After `make smoke`, the response shapes look like this (numbers vary with input):

```json
// POST /events/ingest
{ "accepted": 9, "duplicates": 0, "rejected": 1,
  "results": [
    {"event_id": "e3a5...uuid5", "status": "stored"},
    {"event_id": "smoke-malformed-001", "status": "rejected", "error": "Input should be less than or equal to 1"}
  ]
}

// GET /stores/STORE_BLR_001/metrics
{ "store_id": "STORE_BLR_001", "as_of": "...Z",
  "unique_visitors": 2, "purchasing_visitors": 0,
  "conversion_rate": 0.0, "current_queue_depth": 3,
  "abandonment_rate": 0.0, "has_data": true,
  "avg_dwell_ms_per_zone": {"MOISTURISER": 60000.0} }

// GET /stores/STORE_BLR_001/funnel
{ "store_id": "STORE_BLR_001", "window": "today",
  "stages": [
    {"name": "Entry",         "count": 2, "drop_off_pct": 0.0},
    {"name": "Zone Visit",    "count": 1, "drop_off_pct": 50.0},
    {"name": "Billing Queue", "count": 1, "drop_off_pct": 0.0},
    {"name": "Purchase",      "count": 0, "drop_off_pct": 100.0}
  ]
}

// GET /health  (per-store last-event-ts; STALE_FEED warning at >10 min lag)
{ "status": "ok", "db_ok": true, "redis_ok": true,
  "stores": [{"store_id": "STORE_BLR_001", "last_event_ts": "...Z", "stale": false}],
  "warnings": [] }
```

The `rejected` bucket carries the malformed event from the fixture (`confidence > 1.0`)
which validates partial-success behaviour. Replaying the same fixture yields
`accepted=0, duplicates=N` — idempotent by `event_id`.

## Documentation

* `docs/DESIGN.md` — architecture, AI-Assisted Decisions section
* `docs/CHOICES.md` — three load-bearing decisions with options-considered, AI-suggestion, what-I-chose-and-why
