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

## Narrated video

`bun run video` builds `dist/uwazi-admin-agent.mp4`: a 1080p30 video that shows
one slide at a time, each for exactly as long as its narration.

```bash
cd slides
bun install
bun run video
```

Requirements: `ffmpeg` and `uv` on `PATH`, plus the Kokoro model files from the
sibling `../text-to-audio` project (`kokoro-v1.0.onnx` and `voices-v1.0.bin`;
override the paths with `KOKORO_MODEL` and `KOKORO_VOICES`). The Slidev export
needs Node 22.12+; if the `node` on `PATH` is older, the script picks the newest
suitable nvm version automatically.

What happens:

1. `dist/slides/NN.png` — one screenshot per slide (`slidev export`).
2. `dist/audio/NN.wav` — Kokoro reads the slide's entry in `narration.yaml`.
3. `dist/uwazi-admin-agent.mp4` — one video segment per slide, concatenated and
   muxed with the narration, normalized to -16 LUFS.

Timing is frame exact: at 24 kHz and 30 fps a frame is exactly 800 samples, so
each slide is padded to a whole number of frames. Cuts never drift from the
audio, and re-running is deterministic.

Edit `narration.yaml` to change the script, voice (`af_heart`, `am_michael`,
`bm_george`, ...), speed, pauses, or per-slide overrides. The `pronunciations`
map rewrites words for the synthesizer only: espeak reads a leading "u" as
"yoo", so `Uwazi: Oowazi` produces oo-WAH-zee.

```bash
uv run scripts/narrate.py --check        # validate coverage, list words per slide
uv run scripts/narrate.py --skip-export  # reuse the PNGs, redo audio and video
uv run scripts/narrate.py --skip-synth   # reuse the WAVs, redo the video only
```

`dist/` is git-ignored: the video stays local and is not part of the published
site.

