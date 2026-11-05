"""VideoDocument: the model-agnostic intermediate representation of a video.

A video becomes a time-ordered list of events (keyframes + transcript
segments). Provider adapters in `vidlayer.render` turn that into whatever
message format a given LLM API expects.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterator, Union

from . import ffmpeg
from .frames import Frame, format_ts, select_keyframes
from .transcript import Segment, load_transcript


@dataclass
class VideoDocument:
    info: ffmpeg.VideoInfo
    frames: list[Frame]
    segments: list[Segment]
    transcript_source: str
    meta: dict = field(default_factory=dict)

    # ---- construction -------------------------------------------------
    @classmethod
    def build(
        cls,
        path: str | Path,
        *,
        transcript: str | Path | None = None,
        max_frames: int = 32,
        max_gap: float | None = None,
        scene_threshold: float = ffmpeg.SCENE_THRESHOLD,
        max_dim: int = 768,
        asr: bool = True,
        asr_model: str = "base",
    ) -> "VideoDocument":
        info = ffmpeg.probe(path)
        frames = select_keyframes(
            path, info, max_frames=max_frames, max_gap=max_gap,
            scene_threshold=scene_threshold, max_dim=max_dim,
        )
        segments, source = load_transcript(path, transcript, allow_asr=asr, asr_model=asr_model, info=info)
        return cls(info=info, frames=frames, segments=segments, transcript_source=source)

    # ---- views --------------------------------------------------------
    def timeline(self) -> Iterator[Union[Frame, list[Segment]]]:
        """Yield frames in order, each followed by the speech that happens until the next frame.

        Speech before the first frame is yielded first. Segments are grouped so
        adapters can emit one text block per gap rather than one per caption.
        """
        segs = sorted(self.segments, key=lambda s: s.start)
        frames = sorted(self.frames, key=lambda f: f.t)
        i = 0
        first_t = frames[0].t if frames else float("inf")
        pre = []
        while i < len(segs) and segs[i].start < first_t:
            pre.append(segs[i]); i += 1
        if pre:
            yield pre
        for k, fr in enumerate(frames):
            yield fr
            nxt = frames[k + 1].t if k + 1 < len(frames) else float("inf")
            group = []
            while i < len(segs) and segs[i].start < nxt:
                group.append(segs[i]); i += 1
            if group:
                yield group

    def header(self) -> str:
        i = self.info
        lines = [
            f"<video source=\"{Path(i.path).name}\" duration=\"{format_ts(i.duration)}\" "
            f"resolution=\"{i.width}x{i.height}\" fps=\"{i.fps}\">",
            "This video is presented as a chronological sequence of keyframes, each labelled with its",
            "timestamp, interleaved with the transcript of what is said between them. Keyframes were",
            "chosen at shot/scene changes plus regular intervals, so on-screen content between two",
            "frames is approximately the earlier frame. Refer to moments by timestamp.",
            f"Keyframes: {len(self.frames)}. Transcript: {self.transcript_source}"
            + ("" if self.segments else " (no speech transcript available; visuals only)") + ".",
        ]
        if i.title:
            lines.insert(1, f"Title: {i.title}")
        return "\n".join(lines)

    footer = "</video>"

    @staticmethod
    def format_segments(segs: list[Segment]) -> str:
        return "\n".join(
            f"[{format_ts(s.start)}] " + (f"{s.speaker}: " if s.speaker else "") + s.text for s in segs
        )

    def transcript_text(self, start: float = 0, end: float = float("inf")) -> str:
        return self.format_segments([s for s in self.segments if s.end >= start and s.start <= end])

    def estimated_claude_tokens(self) -> int:
        text = sum(len(s.text) for s in self.segments) // 4 + 200
        return text + sum(f.claude_tokens for f in self.frames)

    # ---- persistence --------------------------------------------------
    def save(self, out_dir: str | Path) -> Path:
        """Write frames as JPEGs plus a JSON index and a Markdown rendering.

        The Markdown file is directly consumable by agentic tools that can read
        local images (e.g. Claude Code: "read out/video.md and its frames").
        """
        out = Path(out_dir)
        (out / "frames").mkdir(parents=True, exist_ok=True)
        frame_entries = []
        for fr in self.frames:
            name = f"frames/{fr.t:09.2f}.jpg"
            (out / name).write_bytes(fr.jpeg)
            frame_entries.append({**{k: v for k, v in asdict(fr).items() if k != "jpeg"}, "file": name})
        index = {
            "info": asdict(self.info),
            "transcript_source": self.transcript_source,
            "frames": frame_entries,
            "segments": [asdict(s) for s in self.segments],
            "meta": self.meta,
        }
        (out / "video.json").write_text(json.dumps(index, indent=2))
        from .render import to_markdown
        (out / "video.md").write_text(to_markdown(self))
        return out

    @classmethod
    def load(cls, out_dir: str | Path) -> "VideoDocument":
        out = Path(out_dir)
        index = json.loads((out / "video.json").read_text())
        frames = []
        for f in index["frames"]:
            file = f.pop("file")
            frames.append(Frame(jpeg=(out / file).read_bytes(), **f))
        return cls(
            info=ffmpeg.VideoInfo(**index["info"]),
            frames=frames,
            segments=[Segment(**s) for s in index["segments"]],
            transcript_source=index["transcript_source"],
            meta=index.get("meta", {}),
        )


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def video_id(path: str | Path) -> str:
    p = Path(path).resolve()
    st = p.stat()
    return hashlib.sha1(f"{p}:{st.st_size}:{st.st_mtime_ns}".encode()).hexdigest()[:12]
