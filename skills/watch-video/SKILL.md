---
name: watch-video
description: Watch and understand a video file (mp4, mov, mkv, webm, etc.) using the vidlayer MCP tools — keyframes, storyboards, high-res frames, and timestamped transcripts. Use whenever the user references a local video, screen recording, lecture, meeting recording, demo, or asks what happens / is shown / is said in a video.
---

# Watching a video with vidlayer

You can't ingest video directly, but the `vidlayer` MCP server lets you look at one the way a person scrubs through it. Work coarse-to-fine so you spend image tokens only where they matter.

## Workflow

1. **Overview first.** Call `open_video` with the absolute path. You get a `video_id`, metadata, the list of keyframe timestamps (scene changes + intervals), the full transcript, and one **storyboard** image tiling those keyframes with timestamp labels. For many questions this is already enough.
2. **Decide where to look.** Use the transcript and storyboard to find the moments relevant to the user's question (e.g. "here's the slide", a cut to a terminal, an error dialog).
3. **Zoom in on a range.** `get_frames(video_id, start, end, count)` returns `count` evenly spaced frames plus the speech in that range. Use `as_storyboard=true` to get them tiled into one cheaper image when you only need the gist.
4. **Read details.** `get_frame(video_id, t, max_dim=1568)` returns one high-resolution frame — use it for small text, code, terminal output, charts, or UI details. Don't guess at text you can't read; fetch a sharper frame.
5. **Exact words.** `get_transcript(video_id, start, end)` returns the timestamped speech for a range.
6. **Find something.** `find_text(video_id, query)` lists where a word or phrase appears on screen or in speech. Use it before scanning for a term by eye. On-screen text is known only for the keyframes `open_video` chose, so no match doesn't prove something never appeared.
7. **Who said it.** If the question depends on who is speaking, call `open_video` again with `diarize=true`, plus `num_speakers` if you know it. This labels the transcript by voice, but it's slow on long videos and downloads about 33 MB the first time, so skip it otherwise. Labels like `Speaker 1` only separate voices, so don't present them as names.

## Guidelines

- Always cite moments as timestamps (`mm:ss`) so the user can jump there.
- Between two keyframes, the screen is *approximately* the earlier one; if something could have changed (a fast UI interaction, an animation), sample that range with `get_frames` before making claims.
- On-screen text comes from OCR and can contain misread words. Before quoting a number, command, or name from it, check the frame with `get_frame`.
- If `open_video` says on-screen text wasn't read, Tesseract isn't installed. You can still read the frames yourself, and the user can install it to enable text search (`brew install tesseract`, `apt install tesseract-ocr`).
- If `open_video` reports no transcript, the video has no subtitles and no local speech-to-text is installed — say so and rely on visuals. The user can supply an `.srt`/`.vtt` via `transcript_path`, or install speech-to-text with `pip install 'vidlayer[whisper]'`.
- Prefer a few targeted `get_frames`/`get_frame` calls over large `count` values; each image costs roughly (width × height) / 750 tokens.
- Requires `ffmpeg`. If a tool errors with "ffmpeg not found", tell the user to install it (`brew install ffmpeg`, `apt install ffmpeg`, or https://ffmpeg.org/download.html).
