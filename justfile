formatter:
    uv run ruff format --line-length 125 .

lint:
    uv run ruff check . --fix

tag:
	#!/bin/bash
	set -euo pipefail
	CURRENT_DATE=$(date +%Y.%-m.%-d)
	REVISION=1
	while git rev-parse -q --verify "refs/tags/${CURRENT_DATE}.${REVISION}" >/dev/null \
		|| git ls-remote --exit-code --tags origin "refs/tags/${CURRENT_DATE}.${REVISION}" >/dev/null 2>&1; do
		REVISION=$((REVISION + 1))
	done
	NEW_TAG="${CURRENT_DATE}.${REVISION}"
	echo "Creating new tag: $NEW_TAG"
	git tag "$NEW_TAG"
	git push origin "refs/tags/${NEW_TAG}"
	sed -i "s|pip install git+https://github.com/huridocs/python_uwazi_API@.*|pip install git+https://github.com/huridocs/python_uwazi_API@${NEW_TAG}|" README.md
	git add README.md
	git commit -m "update version to ${NEW_TAG}"
	git push

test:
    uv run python -m pytest -v -m "not e2e" --ignore=src/uwazi_property_filler/tests --maxfail=1 --disable-warnings

test_e2e:
    uv run python -m pytest -v -m e2e --ignore=src/uwazi_property_filler/tests --disable-warnings

start:
    docker compose up --build

stop:
    docker compose down