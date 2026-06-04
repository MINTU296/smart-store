# Apex Retail — Store Intelligence

End-to-end pipeline that turns raw CCTV clips into a live store-analytics API.

* **Detection** — YOLOv11n + ByteTrack + colour-histogram Re-ID, per-store
  staff uniform classifier, zone polygons from the supplied store layouts.
* **Event stream** — structured events (PDF-spec schema) POSTed to the API.
* **API** — FastAPI + SQLite + Redis. Endpoints for metrics, funnel, heatmap,
  anomalies, health.
* **Live dashboard** — WebSocket-driven page at `/dashboard/`.

North-star metric: **offline store conversion rate** = visitors who completed
a purchase ÷ unique visitors in the session window.

## Quick start

Three commands, well under the spec's 5-command budget:

```bash
git clone <repo-url> store-intelligence && cd store-intelligence  # 1
cp .env.example .env                                              # 2
make smoke                                                        # 3 — boots stack + ingests fixture + hits every endpoint
```

`make smoke` prints the body of every read endpoint so you can audit the
numbers in one screen. The fixture posts ten events with *now-anchored*
timestamps so today's `/metrics`, `/funnel`, `/heatmap`, and `/anomalies`
all light up. The same script also re-POSTs the batch to prove idempotency
(`duplicates=N` on the second call).

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

| Route | Purpose |
|---|---|
| `POST /events/ingest` | Idempotent event ingestion (≤500 per batch) |
| `GET /stores/{id}/metrics` | Today's unique visitors, conversion rate, queue depth, avg dwell per zone, abandonment rate |
| `GET /stores/{id}/funnel` | Entry → Zone Visit → Billing Queue → Purchase, no re-entry double-counting |
| `GET /stores/{id}/heatmap` | Per-zone visits + dwell, normalised 0..100 |
| `GET /stores/{id}/anomalies` | Active queue spikes, conversion drops, dead zones |
| `GET /stores/{id}/insights` | Per-camera health, occupancy, today-vs-7d deltas, queue trend, traffic-by-hour, attention vs conversion, staff vs customers, session chips |
| `GET /health` | Service liveness + per-store last-event-ts + STALE_FEED warning |
| `WS /ws/{store_id}` | Live event stream (powers the dashboard) |
| `GET /dashboard/` | Live React UI |

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

68 tests, **79 % statement coverage** (well above the 70 % bar). Latest run:

```
Name                   Stmts   Miss  Cover
------------------------------------------
app/funnel.py             31      2    94%
app/heatmap.py            25      2    92%
app/insights.py          263     27    90%
app/main.py               51      4    92%
app/models.py            162      0   100%
app/pos.py                93     11    88%
app/ingestion.py          84     13    85%
app/metrics.py            54      9    83%
app/health.py             44      8    82%
app/anomalies.py          82     23    72%
app/db.py                180     64    64%
pipeline/staff.py        116     13    89%
pipeline/emit.py          61      9    85%
pipeline/reid.py          63     13    79%
pipeline/run.py          431    184    57%   # YOLO branches need real video
pipeline/zones.py         51      9    82%
pipeline/session.py       27      0   100%
pipeline/config.py        19      0   100%
------------------------------------------
TOTAL                   1932    403    79%
```

`pipeline/run.py` and `pipeline/detect.py` have lower coverage because their
YOLO+ByteTrack branches need real video to exercise — verified end-to-end via
`make pipeline` against the supplied clips, not via pytest. `app/db.py` and
`app/ws.py` similarly cover the pure-logic paths via tests and the
infrastructure paths via `make smoke`.

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
  "metadata":   {"queue_depth": null, "sku_zone": null, "session_seq": 1}
}
```

`event_id` is a deterministic uuid5 of `(store, cam, visitor, type, ts)`, so
re-running the pipeline against the same clips produces *the same* event_ids
and the API treats the second run as a no-op (`accepted=0, duplicates=N`).

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
