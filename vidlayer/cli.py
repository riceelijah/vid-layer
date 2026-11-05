"""Command-line interface.

  vidlayer build VIDEO [-o DIR]        -> DIR/video.md + frames/ + video.json
  vidlayer render VIDEO --format F     -> JSON content blocks for an LLM API on stdout
  vidlayer ask VIDEO "question"        -> send the video to Claude and print the answer
  vidlayer mcp                         -> run the MCP server on stdio
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .document import VideoDocument
from .ffmpeg import SCENE_THRESHOLD
from .render import ADAPTERS


def _add_build_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("video", help="path to a video file (or a URL if yt-dlp is installed)")
    p.add_argument("--transcript", help="SRT/VTT file to use instead of auto-detection")
    p.add_argument("--max-frames", type=int, default=32, help="keyframe budget (default 32)")
    p.add_argument("--max-gap", type=float, help="max seconds between keyframes (default: duration/max-frames)")
    p.add_argument("--scene-threshold", type=float, default=SCENE_THRESHOLD, help="0-1; lower = more sensitive (default %(default)s)")
    p.add_argument("--max-dim", type=int, default=768, help="longest side of each frame in px (default 768)")
    p.add_argument("--no-asr", action="store_true", help="don't run speech-to-text if no subtitles are found")
    p.add_argument("--asr-model", default="base", help="whisper model size (default base)")


def _resolve_input(src: str, workdir: Path) -> Path:
    if not src.startswith(("http://", "https://")):
        return Path(src)
    try:
        import yt_dlp  # type: ignore
    except ImportError:
        sys.exit("URL input needs yt-dlp: pip install 'vidlayer[url]'")
    workdir.mkdir(parents=True, exist_ok=True)
    opts = {
        "outtmpl": str(workdir / "%(id)s.%(ext)s"),
        "format": "bv*[height<=720]+ba/b[height<=720]/b",
        "merge_output_format": "mp4",
        "writesubtitles": True, "writeautomaticsub": True,
        "subtitleslangs": ["en.*", "en"], "subtitlesformat": "vtt",
        "quiet": True, "noprogress": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(src, download=True)
        return Path(ydl.prepare_filename(info)).with_suffix(".mp4")


def _build(args: argparse.Namespace, workdir: Path) -> VideoDocument:
    path = _resolve_input(args.video, workdir)
    doc = VideoDocument.build(
        path, transcript=args.transcript, max_frames=args.max_frames, max_gap=args.max_gap,
        scene_threshold=args.scene_threshold, max_dim=args.max_dim,
        asr=not args.no_asr, asr_model=args.asr_model,
    )
    print(
        f"vidlayer: {len(doc.frames)} keyframes, {len(doc.segments)} transcript segments "
        f"({doc.transcript_source}), ~{doc.estimated_claude_tokens():,} Claude tokens",
        file=sys.stderr,
    )
    return doc


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="vidlayer", description="Make videos readable by LLMs.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="write a Markdown + frames bundle to a directory")
    _add_build_args(b)
    b.add_argument("-o", "--out", help="output directory (default: <video-name>.vidlayer)")

    r = sub.add_parser("render", help="print provider-specific JSON content blocks")
    _add_build_args(r)
    r.add_argument("--format", choices=sorted(ADAPTERS), default="anthropic")

    a = sub.add_parser("ask", help="ask Claude a question about a video (needs ANTHROPIC_API_KEY)")
    _add_build_args(a)
    a.add_argument("question")
    a.add_argument("--model", default="claude-opus-5-5")
    a.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"])

    sub.add_parser("mcp", help="run the MCP server over stdio")

    args = ap.parse_args(argv)
    if args.cmd == "mcp":
        from .mcp_server import main as mcp_main
        return mcp_main()

    workdir = Path.home() / ".cache" / "vidlayer" / "downloads"
    if args.cmd == "build":
        doc = _build(args, workdir)
        out = Path(args.out or Path(args.video).stem + ".vidlayer") if not args.video.startswith("http") else Path(args.out or "video.vidlayer")
        doc.save(out)
        print(out / "video.md")
    elif args.cmd == "render":
        json.dump(ADAPTERS[args.format](_build(args, workdir)), sys.stdout)
        print()
    elif args.cmd == "ask":
        try:
            import anthropic  # type: ignore
        except ImportError:
            sys.exit("`ask` needs the Anthropic SDK: pip install 'vidlayer[claude]'")
        from .render import to_anthropic
        content = to_anthropic(_build(args, workdir)) + [{"type": "text", "text": args.question}]
        client = anthropic.Anthropic()
        with client.beta.messages.stream(
            model=args.model,
            max_tokens=64000,
            thinking={"type": "adaptive"},
            output_config={"effort": args.effort},
            # On a safety-classifier decline, let the API re-run on a fallback model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            messages=[{"role": "user", "content": content}],
        ) as stream:
            for chunk in stream.text_stream:
                print(chunk, end="", flush=True)
            final = stream.get_final_message()
        print()
        if final.stop_reason == "refusal":
            sys.exit("vidlayer: the model declined this request.")
        if final.stop_reason == "max_tokens":
            print("vidlayer: answer truncated at max_tokens.", file=sys.stderr)

if __name__ == "__main__":
    main()
