formatter:
    uv run ruff format --line-length 125 .

tag:
	#!/bin/bash
	CURRENT_DATE=$(date +%Y.%-m.%-d)
	LATEST_TAG=$(git tag --list "${CURRENT_DATE}.*" --sort=-version:refname | head -n1)
	if [ -z "$LATEST_TAG" ]; then
		REVISION=1
	else
		REVISION=$(echo $LATEST_TAG | cut -d. -f4)
		REVISION=$((REVISION + 1))
	fi
	NEW_TAG="${CURRENT_DATE}.${REVISION}"
	echo "Creating new tag: $NEW_TAG"
	git tag $NEW_TAG
	git push --tag
	sed -i "s|pip install git+https://github.com/huridocs/python_uwazi_API@.*|pip install git+https://github.com/huridocs/python_uwazi_API@${NEW_TAG}|" README.md
	git add README.md
	git commit -m "update version to ${NEW_TAG}"
	git push

test:
    uv run python -m pytest -v --maxfail=1 --disable-warnings

start:
    docker compose up --build