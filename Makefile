.PHONY: help install test compile build up down restart logs health clean

PYTHON ?= python3
COMPOSE ?= docker compose
# Windows venvs put binaries in Scripts/, POSIX in bin/.
VENV_BIN := $(if $(wildcard .venv/Scripts),.venv/Scripts,.venv/bin)

help:
	@echo "install   create venv and install runtime + dev dependencies"
	@echo "test      run the test suite"
	@echo "compile   byte-compile every source file"
	@echo "build     build the container image"
	@echo "up        start the stack in the background"
	@echo "down      stop the stack"
	@echo "restart   recreate the container (needed after .env changes)"
	@echo "logs      follow container logs"
	@echo "health    probe /health"
	@echo "clean     remove caches and build artifacts"

install:
	$(PYTHON) -m venv .venv
	$(VENV_BIN)/python -m pip install --upgrade pip
	$(VENV_BIN)/pip install -r requirements.txt -r requirements-dev.txt

test:
	$(PYTHON) -m pytest

compile:
	$(PYTHON) -m compileall -q src key_rotate_proxy.py

build:
	$(COMPOSE) build

up:
	$(COMPOSE) up -d --build

down:
	$(COMPOSE) down

restart:
	$(COMPOSE) up -d --force-recreate

logs:
	$(COMPOSE) logs -f --tail=100

health:
	@curl -fsS "http://127.0.0.1:$${PROXY_HOST_PORT:-3001}/health" || echo "unreachable"

clean:
	rm -rf .pytest_cache
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
	find . -name '*.pyc' -delete
