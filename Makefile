.PHONY: init check test integration up down status logs pull

init:
	uv run --locked power-monitor init

check:
	uv run --locked prek run --all-files

test:
	uv run --locked pytest

integration:
	RUN_STACK_INTEGRATION=1 uv run --locked pytest -n 0 -m integration

up:
	uv run --locked power-monitor up

down:
	uv run --locked power-monitor down

status:
	uv run --locked power-monitor status

logs:
	uv run --locked power-monitor logs --follow

pull:
	uv run --locked power-monitor pull
