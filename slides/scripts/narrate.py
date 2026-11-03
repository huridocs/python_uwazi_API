#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "kokoro-onnx>=0.6.1",
#   "numpy>=1.26",
#   "pyyaml>=6.0",
#   "soundfile>=0.14",
# ]
# ///
"""Build a narrated MP4 from the Slidev deck.

The pipeline has three stages:

1. Synthesize one WAV per slide with Kokoro (same model as ../text-to-audio).
2. Export the deck to one PNG per slide with `slidev export`.
3. Turn every slide into a video segment whose length is its narration length,
   then concatenate the segments and mux the audio.

Timing is frame exact: at 24 kHz and 30 fps one frame is exactly 800 samples,
so each slide is padded to a whole number of frames and the audio cannot drift
against the cuts.

Usage:
    bun run video                 # full pipeline
    uv run scripts/narrate.py --check
    uv run scripts/narrate.py --skip-export
    uv run scripts/narrate.py --skip-synth --skip-export
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf
import yaml

SLIDES_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SLIDES_DIR.parent
DIST = SLIDES_DIR / "dist"
SLIDES_PNG_DIR = DIST / "slides"
AUDIO_DIR = DIST / "audio"
SEGMENT_DIR = DIST / "segments"

FPS = 30
WIDTH, HEIGHT = 1920, 1080
MIN_NODE = (22, 12)
LOUDNESS = "loudnorm=I=-16:TP=-1.5:LRA=11"

DEFAULTS = {
    "voice": "af_heart",
    "speed": 1.0,
    "sentence_pause": 0.34,
    "clause_pause": 0.12,
    "lead": 0.5,
    "tail": 1.0,
    "continuous": True,
}


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


@dataclass
class SlideLine:
    number: int
    text: str
    voice: str
    speed: float


@dataclass
class Narration:
    voice: str
    speed: float
    sentence_pause: float
    clause_pause: float
    lead: float
    tail: float
    continuous: bool
    pronunciations: dict[str, str]
    slides: list[SlideLine]

    def for_slide(self, number: int) -> SlideLine:
        return self.slides[number - 1]


def load_narration(path: Path) -> Narration:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SystemExit(f"{path.name}: expected a mapping at the top level")

    config = {key: raw.get(key, value) for key, value in DEFAULTS.items()}
    pronunciations = {str(k): str(v) for k, v in (raw.get("pronunciations") or {}).items()}

    missing = [key for key in ("slides",) if key not in raw]
    if missing:
        raise SystemExit(f"{path.name}: missing key(s): {', '.join(missing)}")

    entries = raw["slides"] or {}
    if not isinstance(entries, dict) or not entries:
        raise SystemExit(f"{path.name}: `slides` must be a non-empty mapping of slide number to text")

    numbers = sorted(int(number) for number in entries)
    expected = list(range(1, len(numbers) + 1))
    if numbers != expected:
        raise SystemExit(f"{path.name}: slide numbers must run 1..{len(numbers)} without gaps, got {numbers}")

    slides: list[SlideLine] = []
    for number in numbers:
        entry = entries[number]
        if isinstance(entry, str):
            text, overrides = entry, {}
        elif isinstance(entry, dict):
            text, overrides = entry.get("text", ""), entry
        else:
            raise SystemExit(f"{path.name}: slide {number} must be a string or a mapping")

        text = text.strip()
        if not text:
            raise SystemExit(f"{path.name}: slide {number} has no narration text")

        slides.append(
            SlideLine(
                number=number,
                text=text,
                voice=str(overrides.get("voice", config["voice"])),
                speed=float(overrides.get("speed", config["speed"])),
            )
        )

    return Narration(pronunciations=pronunciations, slides=slides, **config)


def apply_pronunciations(text: str, mapping: dict[str, str]) -> str:
    """Word-boundary, case-insensitive replacements applied before synthesis."""
    for source in sorted(mapping, key=len, reverse=True):
        text = re.sub(rf"\b{re.escape(source)}\b", mapping[source], text, flags=re.IGNORECASE)
    return text


def paragraphs_of(text: str) -> list[str]:
    blocks = [block for block in re.split(r"\n\s*\n", text.strip()) if block.strip()]
    return [" ".join(block.split()) for block in blocks]


# --------------------------------------------------------------------------- #
# Tooling
# --------------------------------------------------------------------------- #


def run(cmd: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    printable = " ".join(cmd)
    print(f"  $ {printable}")
    result = subprocess.run(cmd, cwd=cwd, env=env)
    if result.returncode != 0:
        raise SystemExit(f"command failed ({result.returncode}): {printable}")


def node_version(path: str) -> tuple[int, int]:
    out = subprocess.run([path, "-v"], capture_output=True, text=True, check=True).stdout
    match = re.match(r"v(\d+)\.(\d+)", out.strip())
    if not match:
        return (0, 0)
    return int(match.group(1)), int(match.group(2))


def node_env() -> dict[str, str]:
    """Return an environment whose `node` is new enough for Slidev."""
    env = dict(os.environ)
    current = shutil.which("node")
    if current and node_version(current) >= MIN_NODE:
        return env

    candidates: list[tuple[tuple[int, int], Path]] = []
    for candidate in Path.home().glob(".nvm/versions/node/v*/bin/node"):
        candidates.append((node_version(str(candidate)), candidate))

    for version, candidate in sorted(candidates, reverse=True):
        if version >= MIN_NODE:
            print(f"  using node {version[0]}.{version[1]} from {candidate.parent}")
            env["PATH"] = f"{candidate.parent}:{env.get('PATH', '')}"
            return env

    minimum = ".".join(str(part) for part in MIN_NODE)
    raise SystemExit(f"Slidev export needs node >= {minimum}. Install it or put a newer node on PATH.")


def probe_duration(path: Path) -> float:
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return float(out)


# --------------------------------------------------------------------------- #
# Stage 1: narration audio
# --------------------------------------------------------------------------- #


def synthesize(
    kokoro,
    line: SlideLine,
    narration: Narration,
    continuous: bool,
) -> tuple[np.ndarray, int]:
    text = apply_pronunciations(line.text, narration.pronunciations)
    parts: list[np.ndarray] = []
    sample_rate = 24000

    for index, paragraph in enumerate(paragraphs_of(text)):
        audio, sample_rate = kokoro.create(
            paragraph,
            voice=line.voice,
            speed=line.speed,
            sentence_pause=narration.sentence_pause,
            clause_pause=narration.clause_pause,
            continuous=continuous,
        )
        parts.append(np.asarray(audio, dtype=np.float32))
        if index > 0:
            gap = np.zeros(int(0.45 * sample_rate), dtype=np.float32)
            parts.append(gap)

    return np.concatenate(parts), sample_rate


def build_audio(narration: Narration, skip_synth: bool) -> tuple[list[int], list[float]]:
    from kokoro_onnx import Kokoro

    model = Path(os.environ.get("KOKORO_MODEL", REPO_ROOT.parent / "text-to-audio" / "kokoro-v1.0.onnx"))
    voices = Path(os.environ.get("KOKORO_VOICES", REPO_ROOT.parent / "text-to-audio" / "voices-v1.0.bin"))

    AUDIO_DIR.mkdir(parents=True, exist_ok=True)

    samples: list[np.ndarray] = []
    frames: list[int] = []
    durations: list[float] = []
    sample_rate = 24000
    spf = 800

    kokoro = None
    continuous = narration.continuous
    if not skip_synth:
        if not model.exists() or not voices.exists():
            raise SystemExit(
                f"Kokoro model files not found:\n  {model}\n  {voices}\nSet KOKORO_MODEL and KOKORO_VOICES to override."
            )
        print(f"  loading Kokoro ({model.name})")
        kokoro = Kokoro(str(model), str(voices))
        if continuous and not getattr(kokoro, "has_timings", False):
            print("  model has no timings; falling back to batched synthesis")
            continuous = False

    for line in narration.slides:
        wav_path = AUDIO_DIR / f"{line.number:02d}.wav"

        if skip_synth:
            audio, sample_rate = sf.read(wav_path, dtype="float32")
            audio = np.asarray(audio, dtype=np.float32)
        else:
            audio, sample_rate = synthesize(kokoro, line, narration, continuous)

        if sample_rate % FPS:
            raise SystemExit(f"sample rate {sample_rate} is not divisible by {FPS} fps")
        spf = sample_rate // FPS

        if not skip_synth:
            # Raw speech only; padding is applied below so it is identical on
            # every run, including --skip-synth re-assemblies.
            sf.write(wav_path, audio, sample_rate, subtype="PCM_16")

        lead = np.zeros(int(round(narration.lead * sample_rate)), dtype=np.float32)
        tail = np.zeros(int(round(narration.tail * sample_rate)), dtype=np.float32)
        padded = np.concatenate([lead, audio, tail])

        count = max(1, math.ceil(len(padded) / spf))
        padded = np.pad(padded, (0, count * spf - len(padded)))

        samples.append(padded)
        frames.append(count)
        durations.append(count / FPS)
        print(f"  slide {line.number:>2}: {count / FPS:6.2f}s  {len(line.text.split()):>3} words")

    audio = np.concatenate(samples)
    sf.write(DIST / "narration.wav", audio, sample_rate, subtype="PCM_16")
    return frames, durations


# --------------------------------------------------------------------------- #
# Stage 2: slide images
# --------------------------------------------------------------------------- #


def export_slides(env: dict[str, str]) -> None:
    if not shutil.which("bun"):
        raise SystemExit("bun not found: install it from https://bun.sh/")
    print("  exporting slides to PNG")
    run(["bun", "run", "export:png"], cwd=SLIDES_DIR, env=env)


def slide_images(expected: int) -> list[Path]:
    if not SLIDES_PNG_DIR.is_dir():
        raise SystemExit(f"{SLIDES_PNG_DIR} does not exist; run without --skip-export first")

    images = sorted(SLIDES_PNG_DIR.glob("*.png"), key=lambda path: int(path.stem))
    if len(images) != expected:
        raise SystemExit(
            f"found {len(images)} slide images but narration.yaml has {expected} slides; "
            "update both (or delete dist/slides and re-export)"
        )
    return images


# --------------------------------------------------------------------------- #
# Stage 3: video
# --------------------------------------------------------------------------- #


def encode_segment(image: Path, frames: int, out: Path) -> None:
    run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-loop",
            "1",
            "-framerate",
            str(FPS),
            "-i",
            str(image),
            "-frames:v",
            str(frames),
            "-vf",
            f"scale={WIDTH}:{HEIGHT}:flags=lanczos,format=yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "19",
            "-r",
            str(FPS),
            "-an",
            str(out),
        ]
    )


def assemble(images: list[Path], frames: list[int], output: Path) -> None:
    if SEGMENT_DIR.exists():
        shutil.rmtree(SEGMENT_DIR)
    SEGMENT_DIR.mkdir(parents=True)

    segments: list[Path] = []
    for image, count in zip(images, frames):
        segment = SEGMENT_DIR / f"{image.stem}.mp4"
        encode_segment(image, count, segment)
        segments.append(segment)

    listing = SEGMENT_DIR / "list.txt"
    listing.write_text("".join(f"file '{segment.name}'\n" for segment in segments), encoding="utf-8")

    silent = DIST / "video-silent.mp4"
    run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", listing.name, "-c", "copy", str(silent)],
        cwd=SEGMENT_DIR,
    )

    # Kokoro peaks around -7 dBFS; bring speech to a normal online-video level.
    normalized = DIST / "narration-normalized.wav"
    run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(DIST / "narration.wav"),
            "-af",
            LOUDNESS,
            "-ar",
            "48000",
            str(normalized),
        ]
    )

    run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(silent),
            "-i",
            str(normalized),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            "-shortest",
            str(output),
        ]
    )


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("config", nargs="?", default=str(SLIDES_DIR / "narration.yaml"))
    parser.add_argument("--output", default=str(DIST / "uwazi-admin-agent.mp4"))
    parser.add_argument("--skip-export", action="store_true", help="reuse the PNGs in dist/slides")
    parser.add_argument("--skip-synth", action="store_true", help="reuse the WAVs in dist/audio")
    parser.add_argument("--check", action="store_true", help="validate the script and exit")
    args = parser.parse_args(argv)

    narration = load_narration(Path(args.config))
    words = sum(len(line.text.split()) for line in narration.slides)

    print(
        f"{Path(args.config).name}: {len(narration.slides)} slides, {words} words, "
        f"voice {narration.voice} at speed {narration.speed}"
    )

    if args.check:
        for line in narration.slides:
            print(f"  {line.number:>2}: {len(line.text.split()):>3} words  {line.text[:60]}...")
        print("ok")
        return 0

    DIST.mkdir(parents=True, exist_ok=True)
    output = Path(args.output)

    # Slide count must match before spending time on synthesis.
    if not args.skip_export:
        export_slides(node_env())
    images = slide_images(len(narration.slides))

    print("synthesizing narration")
    frames, durations = build_audio(narration, args.skip_synth)

    print("assembling video")
    assemble(images, frames, output)

    video = probe_duration(output)
    audio = probe_duration(DIST / "narration.wav")
    size_mb = output.stat().st_size / (1024 * 1024)
    print(f"\n{output}\n  {video / 60:.1f} min, {size_mb:.0f} MB, video/audio delta {abs(video - audio):.3f}s")

    (DIST / "build.json").write_text(
        json.dumps(
            {
                "video": str(output),
                "duration": video,
                "fps": FPS,
                "slides": [
                    {"slide": index + 1, "seconds": duration, "frames": count}
                    for index, (duration, count) in enumerate(zip(durations, frames))
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
