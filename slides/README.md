# Uwazi Admin Agent deck

Slidev source for the deck published from the repository's `docs/` folder.

Requires [Bun](https://bun.sh/) (Slidev 53 also needs a Node.js 22.12+ runtime).

```bash
cd slides
bun install
bun run dev      # local preview
bun run build    # builds the static site into ../docs
```

The build uses `--base /python_uwazi_API/`, matching the GitHub Pages project
URL (`https://huridocs.github.io/python_uwazi_API/`). Change the base in
`package.json` if the repository is renamed or served from a custom domain.

Screenshots under `images/` were captured from:

- Uwazi at `http://localhost:3000`
- the admin agent UI at `http://172.21.0.1:5055/app`
