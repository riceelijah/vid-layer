"""Thin wrappers around the ffmpeg / ffprobe binaries."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class FFmpegError(RuntimeError):
    pass


# GUI apps (e.g. launched from the macOS Dock) often get a minimal PATH that
# misses Homebrew and other common install locations.
_FALLBACK_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin", "/usr/bin", "/snap/bin")


def _require(binary: str) -> str:
    path = shutil.which(binary)
    if not path:
        path = next((str(Path(d) / binary) for d in _FALLBACK_DIRS if (Path(d) / binary).is_file()), None)
    if not path:
        raise FFmpegError(f"`{binary}` not found on PATH. Install ffmpeg (https://ffmpeg.org/download.html).")
    return path


def _run(args: list[str], *, binary_output: bool = False) -> subprocess.CompletedProcess:
    proc = subprocess.run(args, capture_output=True, text=not binary_output)
    if proc.returncode != 0:
        err = proc.stderr if isinstance(proc.stderr, str) else proc.stderr.decode(errors="replace")
        raise FFmpegError(f"{Path(args[0]).name} failed ({proc.returncode}): {err.strip()[-2000:]}")
    return proc


@dataclass
class VideoInfo:
    path: str
    duration: float
    width: int
    height: int
    fps: float
    has_audio: bool
    subtitle_streams: int
    title: str | None = None


def probe(path: str | Path) -> VideoInfo:
    out = _run([
        _require("ffprobe"), "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ]).stdout
    data = json.loads(out)
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise FFmpegError(f"No video stream in {path}")
    fmt = data.get("format", {})
    duration = float(fmt.get("duration") or video.get("duration") or 0.0)
    num, _, den = (video.get("avg_frame_rate") or "0/1").partition("/")
    fps = float(num) / float(den) if den and float(den) else 0.0
    return VideoInfo(
        path=str(path),
        duration=duration,
        width=int(video.get("width", 0)),
        height=int(video.get("height", 0)),
        fps=round(fps, 3),
        has_audio=any(s.get("codec_type") == "audio" for s in streams),
        subtitle_streams=sum(1 for s in streams if s.get("codec_type") == "subtitle"),
        title=(fmt.get("tags") or {}).get("title"),
    )


# Hard cuts score well above this and slide changes on a shared background land
# around 0.04-0.2. Going lower turns continuous motion into false cuts.
# ponytail: frame-difference scores can't see small edits like one new bullet
# (about 0.01); those rely on interval sampling. OCR or a text diff would catch them.
SCENE_THRESHOLD = 0.03

_PTS_RE = re.compile(r"pts_time:([0-9.]+)")
_SCORE_RE = re.compile(r"lavfi\.scene_score=([0-9.]+)")


def scene_changes(path: str | Path, threshold: float = SCENE_THRESHOLD, analysis_fps: float = 4.0) -> list[tuple[float, float]]:
    """Return [(time_seconds, scene_score)] for detected shot changes.

    The video is downsampled (fps + resolution) before scoring, which keeps this
    fast even on long inputs.
    """
    vf = (
        f"fps={analysis_fps},scale=320:-2,"
        f"select='gt(scene\\,{threshold})',metadata=print:file=-"
    )
    proc = _run([
        _require("ffmpeg"), "-hide_banner", "-nostats", "-i", str(path),
        "-vf", vf, "-an", "-sn", "-f", "null", "-",
    ])
    # metadata=print writes "frame:N pts:X pts_time:T" followed by "lavfi.scene_score=S"
    results: list[tuple[float, float]] = []
    current_t: float | None = None
    for line in proc.stdout.splitlines():
        m = _PTS_RE.search(line)
        if m:
            current_t = float(m.group(1))
            continue
        m = _SCORE_RE.search(line)
        if m and current_t is not None:
            results.append((current_t, float(m.group(1))))
            current_t = None
    return results


def extract_frame(path: str | Path, t: float, max_dim: int = 768, quality: int = 4) -> bytes:
    """Grab a single JPEG frame at time t, scaled so its longest side <= max_dim."""
    scale = f"scale='if(gt(iw,ih),min({max_dim},iw),-2)':'if(gt(iw,ih),-2,min({max_dim},ih))'"
    # Seeking past the last frame decodes nothing (t is near the end, or audio
    # outlasts the video), so step back until a frame comes out.
    detail = ""
    for seek in dict.fromkeys(max(t - back, 0.0) for back in (0, 0.25, 1, 4, 16)):
        try:
            proc = _run([
                _require("ffmpeg"), "-hide_banner", "-loglevel", "error",
                "-ss", f"{seek:.3f}", "-i", str(path),
                "-frames:v", "1", "-vf", scale, "-q:v", str(quality),
                "-f", "image2pipe", "-vcodec", "mjpeg", "-",
            ], binary_output=True)
        except FFmpegError as exc:
            detail = f": {exc}"
            continue
        if proc.stdout:
            return proc.stdout
    raise FFmpegError(f"No frame decoded at t={t:.2f}s{detail}")


def extract_subtitles(path: str | Path, stream_index: int = 0) -> str | None:
    """Return the embedded subtitle track as SRT text, or None if unavailable."""
    try:
        proc = _run([
            _require("ffmpeg"), "-hide_banner", "-loglevel", "error", "-i", str(path),
            "-map", f"0:s:{stream_index}", "-f", "srt", "-",
        ])
    except FFmpegError:
        return None
    return proc.stdout or None


def extract_audio_wav(path: str | Path, out_path: str | Path) -> Path:
    """16 kHz mono WAV, the format speech models expect."""
    _run([
        _require("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", "-i", str(path),
        "-vn", "-ac", "1", "-ar", "16000", "-f", "wav", str(out_path),
    ])
    return Path(out_path)
