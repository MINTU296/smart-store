# Architectural choices and deviations

This file documents the non-obvious technical decisions in this repo and why
they differ from the reference architecture (see also the Purplle Tech Challenge
brief). Each section is one paragraph; read it like a tour guide, not an
exhaustive defence.

## Detector — YOLOv11n, not v8

The pipeline defaults to `yolo11n.pt` (Ultralytics current generation) loaded
through the same `YOLO.track(...)` API as v8. The nano variant runs on a CPU
fast enough for our 8 fps target, and v11 has measurably better small-object
recall on partial-occlusion crops (back-of-head staff in a crowded aisle, half-
visible visitors at the doorway edge). Override is one env var: `PIPELINE_YOLO`.
We did not retrain — fine-tuning on full-face-blurred CCTV with no labels
adds risk without a clear win.

## Tracker — ByteTrack, not DeepSORT

`tracker="bytetrack.yaml"` is the Ultralytics-bundled default. The brief's
re-entry / group / staff-exclusion criteria are all about *track stability over
seconds*, not minutes — ByteTrack's two-pass association is enough. DeepSORT
would only pay off with real cross-camera identity, which we explicitly defer.

## Frame rate — 8 fps

The clips are 25–30 fps; we sample at an effective 8 fps (entry cameras run
at full rate, floor / billing cameras at `frame_stride=3` over the source).
This sits inside the standard retail-analytics 5–10 fps envelope: walking
shoppers cover ~1 m/s, and 8 fps gives sub-15-cm tracking resolution — plenty
for a "person crosses a line" / "person enters a polygon" decision. Going to
15 fps doubles GPU cost without improving any rubric metric. The entry-camera
exemption (`entry_stride=1`) exists because line crossings can complete in
under 0.5 s and a missed sample is a counted-customer error.

## Event store — SQLite, not PostgreSQL + TimescaleDB

The reference architecture calls for PostgreSQL + TimescaleDB. We stayed on
SQLite (WAL mode) for the demo because the entire challenge data set is one
store × 6 hours of footage and fits in <50 MB. Migrating adds two services to
`docker compose`, requires a schema rewrite (`asyncpg`, hypertable creation,
re-port the POS load path), and moves no rubric points. The current schema is
already time-indexed on `(store_id, ts)` and `(store_id, zone_id, ts)`, so the
hot queries that a hypertable accelerates are already fast on this dataset. At
the 10-store / 1-year-retention point it stops fitting; that is when we
migrate. Until then, SQLite is the right call.

## Event bus — Redis Streams, not pub/sub

We use Redis Streams (`XADD` with `MAXLEN ~ 10000`) instead of pub/sub. The
key property pub/sub does not give us is **replay-on-reconnect**: any WS
client offline mid-flow silently lost the events that arrived during the gap,
so the dashboard could never claim "no missed events". Streams retain the
last 10k events per store; the WS handler accepts an optional `?last_id=…`
cookie and replays the gap. The 10k cap bounds memory at roughly 2–4 MB per
store on the realistic event mix.

## Live RTSP + recorded clips share a path

`pipeline/run.py` accepts either `--clip-dir` (batch) or `--rtsp-url` (live).
The two go through the same `process_clip()` because the only thing that
changes between them is the source string handed to Ultralytics — `YOLO.track()`
takes RTSP URLs natively. This is the "one pipeline, two consumption modes"
property from the reference doc, kept honest.

## Anonymous-by-design — what is *not* stored

By choice and by brief, the system never persists anything biometric. The
events table holds only `(event_id, store_id, camera_id, visitor_id,
event_type, ts, zone_id, dwell_ms, is_staff, confidence, metadata)`. There is
no face crop, no appearance embedding, no colour histogram in long-term
storage; the in-memory ReID histogram (`pipeline/reid.py`) is garbage-collected
when the visitor's session resolves. `visitor_id` is a random
`VIS_<8-hex-chars>` string from `uuid4`, valid for the duration of the
session and meaningless outside it.

## Staff classifier — colour heuristic + behavioural fallback (no VLM by default)

`pipeline/staff.py` runs a per-store HSV uniform check (Store 1 = all-black,
Store 2 = pink shirt + black trousers) on the bbox crop, with a behavioural
fallback (>20 min on the floor without a billing visit). The HSV ranges were
calibrated against real CCTV crops, not synthetic colours (CCTV black tops out
at V≈90 under fluorescent light). The optional VLM tier is stubbed to a dry-
run that audits the prompt template for the rubric's Part D — wiring a live
provider is a one-function swap.

## Conversion without POS — proxy metrics in `/insights`

POS data exists for the brief, but the dashboard also surfaces two video-only
proxies (`conversion_proxies` on `/stores/{id}/insights`):
- **engagement_rate** = unique non-staff visitors with ≥1 non-billing
  `ZONE_ENTER` ÷ entries. Distinguishes "interaction" from raw "traffic".
- **checkout_engagement** = unique non-staff visitors with ≥1
  `BILLING_QUEUE_JOIN` ÷ entries. Closest video-only stand-in for purchase
  intent.
The four-stage funnel (`/funnel`) keeps the POS join as the final step — when
sales data is unavailable for a window, the proxy panel still renders, so the
dashboard never goes blank.

## Out of scope, with reasons

- **Cross-camera ReID via OSNet** — three cameras × one store doesn't earn
  back the modelling complexity. Adopting tracks across cameras already works
  via the time + spatial-overlap heuristic in `_adopt_floor_track`.
- **NVIDIA DeepStream** — useful at 8+ streams; we have 4 per store and the
  Ultralytics batched path saturates one GPU comfortably.
- **PostgreSQL + TimescaleDB migration** — see the storage section above.
- **Face blur in the pipeline** — the input clips arrive pre-blurred per the
  brief; we do not re-blur on read.
