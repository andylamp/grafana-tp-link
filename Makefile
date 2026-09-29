.PHONY: init check test integration up down reset status logs pull

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

# Destructive: remove this project's containers and data volumes, including monitoring history and Grafana accounts.
reset:
	uv run --locked power-monitor reset --yes

status:
	uv run --locked power-monitor status

logs:
	uv run --locked power-monitor logs --follow

pull:
	uv run --locked power-monitor pull
