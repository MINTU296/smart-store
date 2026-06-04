# Reviewer-friendly entry points. The challenge spec says reviewers spend
# 2 minutes running the system and 2 minutes validating API outputs — `make
# smoke` collapses that into one command and prints every endpoint's
# response body so the reviewer can audit the numbers at a glance.

.PHONY: smoke up down seed test pipeline

smoke:        ## boot stack, post a fresh fixture, hit every read endpoint
	@bash scripts/smoke.sh

up:           ## docker compose up -d
	@docker compose up -d

down:         ## docker compose down
	@docker compose down

seed:         ## post the smoke fixture without booting / asserting
	@python3 scripts/build_smoke_fixture.py | curl -fsS -X POST \
		-H 'Content-Type: application/json' --data-binary @- \
		http://localhost:8000/events/ingest | python3 -m json.tool

test:         ## run the test suite with coverage
	@pytest --cov=app --cov=pipeline -q

pipeline:     ## run the YOLO pipeline against Store 1 inside compose
	@docker compose run --rm pipeline \
		python -m pipeline.run --store STORE_BLR_001 --clip-dir "/raw-data/Store 1"
