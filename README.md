# vid-layer

**A video compatibility layer for LLMs.** Most models can't take video, so the usual workaround is pasting a transcript, which throws away everything you *see*: slides, code on screen, diagrams, demos, who's in the room, what changed when.

`vidlayer` turns a video into something any vision-capable LLM can read: **keyframes chosen where the picture changes, each stamped with its timestamp, interleaved with the transcript on one timeline.** It also runs as an **MCP server**, so the model can *watch* the video itself: skim a storyboard, then zoom in on the moments that matter.

```
<video source="talk.mp4" duration="42:10.0" ...>
[00:00.5] keyframe (start):        [image]
Transcript:
[00:01.2] Today I'll show how we cut p99 latency in half...
[01:12.4] keyframe (scene-change): [image: slide "Before / After" chart]
Transcript:
[01:13.0] As you can see here, the long tail mostly came from...
...
</video>
```

## How it works

1. **Probe** the file with `ffprobe`.
2. **Find shot/scene changes** with ffmpeg's scene detector (run on a downscaled stream, so it's fast).
3. **Plan a frame budget**: take the strongest cuts, then fill long static stretches (talking heads, screen recordings) with interval samples. Interval frames that are near-duplicates of the previous one are dropped.
4. **Get a transcript** from the cheapest source available: a `--transcript` file, a sidecar `video.srt`/`.vtt`, an embedded subtitle track, or local Whisper.
5. **Interleave** frames and speech into one `VideoDocument`, then render it for whichever model you're using.

The `VideoDocument` doesn't depend on any provider. Each adapter (Claude, OpenAI-compatible, Gemini, Markdown) is about 10 lines.

## Install

Requires Python 3.10+ and [ffmpeg](https://ffmpeg.org/download.html) on your PATH.

```bash
pip install -e ".[mcp]"           # core + MCP server
pip install -e ".[all]"           # + Whisper speech-to-text, URL download (yt-dlp), Anthropic SDK
```

## Use it

### 1. Claude Code plugin (recommended)

Needs [`uv`](https://docs.astral.sh/uv/getting-started/installation/) and `ffmpeg` on your PATH. In Claude Code:

```
/plugin install vidlayer --marketplace riceelijah/vid-layer
```

Answer `y` to add the marketplace, pick a scope, and you're done. The plugin bundles the MCP server, which `uvx` runs from the plugin folder with no separate `pip install`, plus a `watch-video` skill that tells Claude when and how to use it. Then just ask:

> *What's the UI bug in ~/Videos/demo.mp4 around the 3-minute mark?*

### 2. Any other MCP client (Claude Desktop, Cursor, …)

```bash
pip install "vidlayer[mcp] @ git+https://github.com/riceelijah/vid-layer"
```

Claude Desktop (`claude_desktop_config.json`):

```json
{ "mcpServers": { "vidlayer": { "command": "vidlayer", "args": ["mcp"] } } }
```

The server exposes four tools:

| Tool | What it gives the model |
|---|---|
| `open_video(path)` | metadata, scene list, full transcript, and a **storyboard** (one image tiling the keyframes with timestamps) |
| `get_frames(id, start, end, count)` | `count` frames evenly spaced over a range, plus the speech in it (or tiled into one image with `as_storyboard`) |
| `get_frame(id, t, max_dim)` | one high-res frame, for reading small text, code, and charts |
| `get_transcript(id, start, end)` | exact words for a range |

Most of the value comes from the MCP modes. The model can start from a cheap overview and spend image tokens only where it needs detail, instead of you guessing a frame rate up front.

### 3. Without MCP (file bundle)

```bash
vidlayer build demo.mp4 -o demo.vidlayer
```

…then: *"Read demo.vidlayer/video.md and the frames it references."* The bundle is plain Markdown + JPEGs, so anything that can read files can use it.

### 4. In your own code (any provider)

```python
from vidlayer import VideoDocument, to_anthropic, to_openai, to_gemini

doc = VideoDocument.build("talk.mp4", max_frames=32)

# Claude
client.messages.create(model="claude-opus-5-5", max_tokens=16000, messages=[
    {"role": "user", "content": to_anthropic(doc) + [{"type": "text", "text": "Summarize the slides."}]}
])

# OpenAI-compatible (OpenAI, OpenRouter, vLLM, Ollama, LM Studio…)
{"role": "user", "content": to_openai(doc) + [{"type": "text", "text": "..."}]}

# Gemini (REST `parts`)
{"role": "user", "parts": to_gemini(doc) + [{"text": "..."}]}
```

### 5. CLI

```bash
vidlayer ask demo.mp4 "What command does she run at the end?"   # needs ANTHROPIC_API_KEY
vidlayer render demo.mp4 --format openai > content.json          # raw content blocks
vidlayer build "https://youtube.com/watch?v=..."                 # needs vidlayer[url]
```

Useful knobs: `--max-frames` (budget), `--max-dim` (frame resolution), `--scene-threshold` (lower = more sensitive), `--transcript subs.vtt`, `--no-asr`.

## Cost

Claude charges roughly `width × height / 750` tokens per image. At the default 768px, a 16:9 frame costs about 440 tokens, so 32 keyframes come to about 14k tokens plus the transcript. `build` prints an estimate. The MCP storyboard packs 16 thumbnails into one image of about 1.2k tokens.

## Roadmap

- [ ] OCR pass on frames, so on-screen text can be searched and fed to text-only models
- [ ] Speaker diarization (who said what)
- [ ] Audio events beyond speech (music, laughter, alarms) via an audio tagger
- [ ] Per-scene captions from a cheap vision model, for text-only LLMs and very long videos
- [ ] Transcript-aware frame selection (sample more densely when the speaker says "look at this")
- [ ] Persistent on-disk cache for the MCP server
- [ ] Clip extraction tool (`get_clip` → short GIF/MP4 for models that accept video)

## Development

```bash
pip install -e ".[dev,mcp]"
pytest
```

Tests generate a synthetic video with ffmpeg, so they don't need any fixtures.
