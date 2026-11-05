"""Keyframe selection: pick the few frames that carry the most visual information."""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from . import ffmpeg


@dataclass
class Frame:
    t: float
    jpeg: bytes = field(repr=False)
    width: int
    height: int
    reason: str  # "scene-change" | "interval" | "start" | "requested"
    score: float = 0.0

    @property
    def claude_tokens(self) -> int:
        """Approximate Claude vision cost: (w * h) / 750."""
        return max(1, round(self.width * self.height / 750))


def dhash(jpeg: bytes, size: int = 8) -> int:
    img = Image.open(io.BytesIO(jpeg)).convert("L").resize((size + 1, size), Image.Resampling.LANCZOS)
    px = list(img.get_flattened_data() if hasattr(img, "get_flattened_data") else img.getdata())
    bits = 0
    for row in range(size):
        for col in range(size):
            left = px[row * (size + 1) + col]
            right = px[row * (size + 1) + col + 1]
            bits = (bits << 1) | (left > right)
    return bits


def _hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def _thumb(jpeg: bytes) -> list[int]:
    img = Image.open(io.BytesIO(jpeg)).convert("RGB").resize((8, 8), Image.Resampling.BILINEAR)
    return [c for px in (img.get_flattened_data() if hasattr(img, "get_flattened_data") else img.getdata()) for c in px]


def near_duplicate(a: bytes, b: bytes, hash_distance: int = 4, color_distance: float = 10.0) -> bool:
    """True if two frames look the same: same structure (dHash) *and* same colours.

    dHash alone only sees gradients, so two flat frames of different colours
    would collide; the thumbnail check catches that.
    """
    if _hamming(dhash(a), dhash(b)) > hash_distance:
        return False
    ta, tb = _thumb(a), _thumb(b)
    return sum(abs(x - y) for x, y in zip(ta, tb)) / len(ta) <= color_distance


def plan_times(
    duration: float,
    cuts: list[tuple[float, float]],
    max_frames: int,
    max_gap: float,
    settle: float = 0.4,
) -> list[tuple[float, str, float]]:
    """Decide *when* to sample, before decoding anything.

    1. Every shot change is a candidate (offset slightly so we land on the new
       shot, not the transition), ranked by scene score.
    2. Greedily accept candidates while keeping a minimum spacing that the
       frame budget allows.
    3. Fill any stretch longer than `max_gap` with interval samples so slow,
       static-looking content (talking heads, screen recordings) is still covered.
    4. If still over budget, thin out evenly.
    """
    if duration <= 0:
        return [(0.0, "start", 0.0)]
    end = max(duration - 0.05, 0.0)
    min_spacing = duration / (max_frames * 2)

    chosen: list[tuple[float, str, float]] = [(min(0.5, end), "start", 1.0)]
    for t, score in sorted(cuts, key=lambda c: -c[1]):
        t = min(t + settle, end)
        if all(abs(t - c[0]) >= min_spacing for c in chosen):
            chosen.append((t, "scene-change", score))
    chosen.sort()

    filled: list[tuple[float, str, float]] = []
    boundaries = chosen + [(end, "_end", 0.0)]
    for (a, reason, score), (b, _, _) in zip(boundaries, boundaries[1:]):
        filled.append((a, reason, score))
        gap = b - a
        if gap > max_gap:
            n = int(gap // max_gap)
            step = gap / (n + 1)
            filled.extend((a + step * k, "interval", 0.0) for k in range(1, n + 1))

    if len(filled) > max_frames:
        # Over budget: keep the start and the strongest cuts (up to half the
        # budget), then spread the remaining slots evenly over everything else.
        strong = sorted((i for i in range(1, len(filled)) if filled[i][2] > 0), key=lambda i: -filled[i][2])
        keep = {0, *strong[: max(0, max_frames // 2 - 1)]}
        rest = [i for i in range(len(filled)) if i not in keep]
        need = max_frames - len(keep)
        if need > 0 and rest:
            step = len(rest) / need
            keep |= {rest[int(k * step)] for k in range(need)}
        filled = [filled[i] for i in sorted(keep)]
    return filled


def select_keyframes(
    path: str | Path,
    info: ffmpeg.VideoInfo,
    *,
    max_frames: int = 32,
    max_gap: float | None = None,
    scene_threshold: float = 0.3,
    max_dim: int = 768,
    dedupe_distance: int = 4,
) -> list[Frame]:
    duration = info.duration
    if max_gap is None:
        # Aim for roughly even coverage if there were no cuts at all.
        max_gap = max(2.0, duration / max(1, max_frames - 1))
    cuts = ffmpeg.scene_changes(path, threshold=scene_threshold) if duration > 1 else []
    plan = plan_times(duration, cuts, max_frames, max_gap)

    frames: list[Frame] = []
    for t, reason, score in plan:
        try:
            data = ffmpeg.extract_frame(path, t, max_dim=max_dim)
        except ffmpeg.FFmpegError:
            continue
        # Interval samples of an unchanged screen add tokens but no information.
        if frames and reason == "interval" and near_duplicate(data, frames[-1].jpeg, dedupe_distance):
            continue
        w, hgt = Image.open(io.BytesIO(data)).size
        frames.append(Frame(t=t, jpeg=data, width=w, height=hgt, reason=reason, score=score))
    return frames


def frames_at(path: str | Path, times: list[float], max_dim: int = 768) -> list[Frame]:
    out = []
    for t in times:
        data = ffmpeg.extract_frame(path, t, max_dim=max_dim)
        w, h = Image.open(io.BytesIO(data)).size
        out.append(Frame(t=t, jpeg=data, width=w, height=h, reason="requested"))
    return out


def format_ts(t: float) -> str:
    t = round(max(0.0, t), 1)  # round first so 59.96 carries to 01:00.0
    h, rem = divmod(int(t), 3600)
    m, s = divmod(rem, 60)
    frac = round((t - int(t)) * 10)
    return f"{h}:{m:02d}:{s:02d}.{frac}" if h else f"{m:02d}:{s:02d}.{frac}"


def storyboard(frames: list[Frame], cols: int = 4, tile_width: int = 384) -> bytes:
    """Tile frames into one labelled contact sheet: a cheap whole-video overview in a single image."""
    if not frames:
        raise ValueError("no frames")
    imgs = [Image.open(io.BytesIO(f.jpeg)).convert("RGB") for f in frames]
    tile_h = max(1, round(tile_width * imgs[0].height / imgs[0].width))
    cols = min(cols, len(imgs))
    rows = (len(imgs) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * tile_width, rows * tile_h), "black")
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.load_default(size=max(12, tile_width // 18))
    except TypeError:  # older Pillow
        font = ImageFont.load_default()
    for i, (img, fr) in enumerate(zip(imgs, frames)):
        x, y = (i % cols) * tile_width, (i // cols) * tile_h
        sheet.paste(img.resize((tile_width, tile_h), Image.Resampling.LANCZOS), (x, y))
        label = format_ts(fr.t)
        box = draw.textbbox((x + 4, y + 4), label, font=font)
        draw.rectangle((box[0] - 3, box[1] - 2, box[2] + 3, box[3] + 2), fill="black")
        draw.text((x + 4, y + 4), label, fill="yellow", font=font)
    buf = io.BytesIO()
    sheet.save(buf, format="JPEG", quality=80)
    return buf.getvalue()
