"""On-screen text, read with the tesseract binary when it's installed."""

from __future__ import annotations

import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import ffmpeg
from .frames import Frame, frames_at

MIN_CONFIDENCE = 60  # below this, tesseract is mostly reading texture in photos and faces
OCR_DIM = 1920  # keyframes can be as small as 512px, which is too small to read slide text
WEAK_CUT = 0.006  # one new bullet scores about 0.012; a static screen scores near 0
MAX_CANDIDATES = 32
MIN_NEW_WORDS = 2  # one new word is usually a misread, not an edit
MIN_GAP = 2.0  # a weak cut just before a kept frame is usually the fade into it
SETTLE = 0.4  # same offset as plan_times, to land after the change


def available() -> bool:
    return ffmpeg._find("tesseract") is not None


def read_text(jpeg: bytes) -> str:
    """Text in an image, one line per line on screen. Empty if nothing is readable."""
    # ponytail: uses tesseract's default language, usually English. Add a language option when
    # someone needs another script; low-confidence words are dropped rather than garbled.
    proc = subprocess.run(
        [ffmpeg._find("tesseract"), "stdin", "stdout", "tsv"], input=jpeg, capture_output=True,
        env={**os.environ, "OMP_THREAD_LIMIT": "1"},  # we run one per core already
    )
    if proc.returncode != 0:
        return ""
    lines: dict[tuple, list[str]] = {}
    for row in proc.stdout.decode(errors="replace").splitlines()[1:]:
        c = row.split("\t")  # level page block par line word left top width height conf text
        if len(c) == 12 and c[0] == "5" and float(c[10]) >= MIN_CONFIDENCE and c[11].strip():
            lines.setdefault(tuple(c[1:5]), []).append(c[11].strip())
    kept = (" ".join(words) for words in lines.values())
    return "\n".join(line for line in kept if sum(ch.isalnum() for ch in line) >= 3)


def _words(text: str) -> set[str]:
    return set(re.findall(r"\w{3,}", text.lower()))


def annotate(
    path: str | Path,
    frames: list[Frame],
    weak_cuts: list[tuple[float, float]],
    *,
    duration: float,
    max_frames: int = 32,
    max_dim: int = 768,
) -> list[Frame]:
    """Read each frame's text, then add frames where the text changed without a visible cut.

    Adding one bullet to a slide scores too low to count as a scene change, and the
    frame after it looks like a duplicate. Lowering the threshold alone makes scrolling
    and motion look like cuts, so weak cuts only count if they show new words.
    """
    def read(t: float) -> str:
        try:
            return read_text(ffmpeg.extract_frame(path, t, max_dim=OCR_DIM))
        except ffmpeg.FFmpegError:
            return ""

    # ponytail: only the strongest weak cuts get read, and a full budget stops additions, so
    # edits can be missed on long videos. Raise --max-frames, or lift these if that matters.
    weak = sorted(weak_cuts, key=lambda c: -c[1])[:MAX_CANDIDATES]
    times = sorted(min(t + SETTLE, max(duration - 0.05, 0.0)) for t, _ in weak)
    with ThreadPoolExecutor() as pool:
        for frame, text in zip(frames, pool.map(read, [f.t for f in frames])):
            frame.text = text
        texts = list(pool.map(read, times))

    out = sorted(frames, key=lambda f: f.t)
    for t, text in zip(times, texts):
        before = [f for f in out if f.t <= t]
        after = [f for f in out if f.t > t]
        if len(out) >= max_frames or not before or (after and after[0].t - t < MIN_GAP):
            continue
        if len(_words(text) - _words(before[-1].text or "")) >= MIN_NEW_WORDS:
            frame = frames_at(path, [t], max_dim=max_dim)[0]
            frame.reason, frame.text = "text-change", text
            out = before + [frame] + after
    return out
