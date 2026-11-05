"""MCP server: lets any MCP-capable model *navigate* a video instead of receiving it all at once.

Typical flow for the model:
  1. open_video(path)              -> metadata, scene list, transcript outline, storyboard image
  2. get_transcript(id, start, end) -> exact words for a region
  3. get_frames(id, start, end, n)  -> zoom in: n frames evenly spaced in a region
  4. get_frame(id, t, max_dim)      -> one high-res frame (read small text, code, charts)

Run: `vidlayer mcp` (stdio). Works with Claude Code, Claude Desktop, and any other MCP client.
"""

from __future__ import annotations

import functools
from pathlib import Path

from .document import VideoDocument, video_id
from .ffmpeg import FFmpegError
from .frames import format_ts, frames_at, storyboard

try:
    from mcp.server.mcpserver import Image, MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # mcp < 2
    from mcp.server.fastmcp import FastMCP as MCPServer, Image  # type: ignore
    from mcp.server.fastmcp.exceptions import ToolError  # type: ignore

server = MCPServer(
    "vidlayer",
    instructions=(
        "Tools for watching videos. Start with open_video to get an overview and storyboard, "
        "then use get_frames / get_frame to look closely at specific moments and get_transcript "
        "for exact speech. Timestamps are in seconds."
    ),
)

_docs: dict[str, VideoDocument] = {}


def _tool(fn):
    """Register fn as a tool, reporting expected failures (bad path, ffmpeg
    missing, unknown id) to the model as readable errors instead of a crash."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (ValueError, FFmpegError, OSError) as exc:
            raise ToolError(str(exc)) from exc
    return server.tool()(wrapper)


def _get(vid: str) -> VideoDocument:
    if vid not in _docs:
        raise ValueError(f"Unknown video_id {vid!r}; call open_video first.")
    return _docs[vid]


def _clamp(doc: VideoDocument, t: float) -> float:
    return min(max(0.0, t), max(0.0, doc.info.duration - 0.05))


@_tool
def open_video(path: str, transcript_path: str | None = None, max_frames: int = 16) -> list:
    """Index a local video file. Returns a video_id, metadata, the list of detected
    scenes, a transcript outline, and a storyboard image of keyframes labelled with timestamps."""
    p = Path(path).expanduser()
    if not p.exists():
        raise ValueError(f"No such file: {p}")
    vid = video_id(p)
    doc = _docs.get(vid)
    # Reuse the cached index unless this call asks for something different.
    transcript_path = transcript_path or (doc.meta["transcript_path"] if doc else None)
    if doc is None or (transcript_path, max_frames) != (doc.meta["transcript_path"], doc.meta["max_frames"]):
        doc = VideoDocument.build(p, transcript=transcript_path, max_frames=max_frames, max_dim=512)
        doc.meta.update(path=str(p), transcript_path=transcript_path, max_frames=max_frames)
        _docs[vid] = doc
    scenes = "\n".join(f"  {format_ts(f.t)}  ({f.reason})" for f in doc.frames)
    outline = doc.transcript_text()
    if len(outline) > 6000:
        outline = outline[:6000] + "\n... (truncated; use get_transcript with a time range)"
    text = (
        f"video_id: {vid}\n{doc.header()}\n\nKeyframes shown in storyboard:\n{scenes}\n\n"
        f"Transcript:\n{outline or '(none)'}\n{doc.footer}"
    )
    return [text, Image(data=storyboard(doc.frames, cols=4, tile_width=384), format="jpeg")]


@_tool
def get_transcript(video_id: str, start: float = 0.0, end: float | None = None) -> str:
    """Transcript text (with timestamps) between start and end seconds."""
    doc = _get(video_id)
    return doc.transcript_text(start, end if end is not None else float("inf")) or "(no speech in this range)"


@_tool
def get_frames(video_id: str, start: float, end: float, count: int = 6, as_storyboard: bool = False) -> list:
    """Look closely at a time range: `count` frames evenly spaced from start to end (max 20).
    Set as_storyboard=True to receive them tiled into a single image (cheaper)."""
    doc = _get(video_id)
    count = max(1, min(int(count), 20))
    start, end = _clamp(doc, start), _clamp(doc, end)
    if end < start:
        start, end = end, start
    times = [start] if count == 1 else [start + (end - start) * k / (count - 1) for k in range(count)]
    frames = frames_at(doc.meta["path"], times, max_dim=768)
    if as_storyboard:
        return [f"{count} frames from {format_ts(start)} to {format_ts(end)}",
                Image(data=storyboard(frames, cols=min(4, count)), format="jpeg")]
    out: list = []
    for f in frames:
        out.append(f"[{format_ts(f.t)}]")
        out.append(Image(data=f.jpeg, format="jpeg"))
    seg = doc.transcript_text(start, end)
    if seg:
        out.append("Transcript for this range:\n" + seg)
    return out


@_tool
def get_frame(video_id: str, t: float, max_dim: int = 1568) -> list:
    """A single frame at time t (seconds) at high resolution — use for reading small text, code, or charts."""
    doc = _get(video_id)
    f = frames_at(doc.meta["path"], [_clamp(doc, t)], max_dim=max(128, min(max_dim, 2048)))[0]
    return [f"[{format_ts(f.t)}] {f.width}x{f.height}", Image(data=f.jpeg, format="jpeg")]


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
