.PHONY: dev down

dev:
	./scripts/dev

down:
	docker compose --profile browsers down
