.PHONY: install test chat stats docker-build docker-chat

install:
	python -m venv .venv && . .venv/bin/activate && pip install -r requirements-dev.txt && pip install -e .

test:
	python -m pytest -q

chat:
	analyst chat --user $${USER_ID:-alice}

stats:
	analyst stats

docker-build:
	docker compose build

docker-chat:
	docker compose run --rm analyst chat --user $${USER_ID:-alice}
