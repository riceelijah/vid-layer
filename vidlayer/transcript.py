"""Transcript sources: subtitle files, embedded subtitle tracks, or local speech-to-text."""

from __future__ import annotations

import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import ffmpeg


@dataclass
class Segment:
    start: float
    end: float
    text: str
    speaker: str | None = None


_TS = r"(\d+):(\d{2}):(\d{2})[.,](\d{1,3})|(\d{2}):(\d{2})[.,](\d{1,3})"
_CUE_RE = re.compile(rf"(?:{_TS})\s*-->\s*(?:{_TS})")
_TAG_RE = re.compile(r"<[^>]+>|\{\\[^}]*\}")
_VOICE_RE = re.compile(r"<v(?:\.[^\s>]*)?\s+([^>]+)>")  # WebVTT speaker tag: <v Ana>hello</v>


def _ts_to_seconds(groups: tuple) -> float:
    h, m, s, ms, m2, s2, ms2 = groups
    if h is not None:
        return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000
    return int(m2) * 60 + int(s2) + int(ms2.ljust(3, "0")) / 1000


def parse_subtitles(text: str) -> list[Segment]:
    """Parse SRT or WebVTT text into segments. Tolerant of either format."""
    segments: list[Segment] = []
    blocks = re.split(r"\r?\n\s*\r?\n", text.strip())
    for block in blocks:
        lines = [ln for ln in block.splitlines() if ln.strip()]
        for i, line in enumerate(lines):
            m = _CUE_RE.search(line)
            if not m:
                continue
            g = m.groups()
            start, end = _ts_to_seconds(g[:7]), _ts_to_seconds(g[7:])
            raw = " ".join(lines[i + 1:])
            voice = _VOICE_RE.search(raw)  # ponytail: a cue with several voices keeps the first
            body = re.sub(r"\s+", " ", _TAG_RE.sub("", raw)).strip()
            if body:
                segments.append(Segment(start, end, body, voice.group(1).strip() if voice else None))
            break
    return _dedupe_rolling(segments)


def _dedupe_rolling(segments: list[Segment]) -> list[Segment]:
    """Auto-generated captions (e.g. YouTube) repeat text across overlapping cues.

    Drop cues whose text is fully contained in the previous one and trim
    prefixes that repeat the previous cue's tail. Only cues touching the previous
    one count: a later "Yes." is someone speaking again, not a rolling caption.
    """
    out: list[Segment] = []
    for seg in segments:
        if out and seg.start <= out[-1].end + 0.1 and seg.speaker == out[-1].speaker:
            prev = out[-1]
            if seg.text == prev.text or seg.text in prev.text:
                prev.end = max(prev.end, seg.end)
                continue
            if seg.text.startswith(prev.text):
                seg = Segment(seg.start, seg.end, seg.text[len(prev.text):].strip(), seg.speaker)
                if not seg.text:
                    continue
        out.append(seg)
    return out


def transcribe(path: str | Path, model_size: str = "base") -> list[Segment] | None:
    """Local speech-to-text. Uses faster-whisper if installed, else openai-whisper, else None."""
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except ImportError:
        WhisperModel = None

    with tempfile.TemporaryDirectory() as tmp:
        wav = ffmpeg.extract_audio_wav(path, Path(tmp) / "audio.wav")
        if WhisperModel is not None:
            model = WhisperModel(model_size, compute_type="int8")
            segs, _info = model.transcribe(str(wav), vad_filter=True)
            return [Segment(s.start, s.end, s.text.strip()) for s in segs if s.text.strip()]
        try:
            import whisper  # type: ignore
        except ImportError:
            return None
        result = whisper.load_model(model_size).transcribe(str(wav))
        return [Segment(s["start"], s["end"], s["text"].strip()) for s in result["segments"] if s["text"].strip()]


def load_transcript(
    video_path: str | Path,
    transcript_path: str | Path | None = None,
    *,
    allow_asr: bool = True,
    asr_model: str = "base",
    info: ffmpeg.VideoInfo | None = None,
) -> tuple[list[Segment], str]:
    """Return (segments, source_description), trying the cheapest source first."""
    if transcript_path:
        tp = Path(transcript_path).expanduser()
        return parse_subtitles(tp.read_text(encoding="utf-8", errors="replace")), f"file:{tp.name}"

    # Sidecar files next to the video: video.srt, video.en.vtt, ... (not video2.srt)
    vp = Path(video_path)
    for candidate in sorted(vp.parent.iterdir()):
        if candidate.name.startswith(vp.stem + ".") and candidate.suffix.lower() in (".srt", ".vtt") and candidate.is_file():
            return parse_subtitles(candidate.read_text(encoding="utf-8", errors="replace")), f"sidecar:{candidate.name}"

    info = info or ffmpeg.probe(video_path)
    if info.subtitle_streams:
        srt = ffmpeg.extract_subtitles(video_path)
        if srt:
            segs = parse_subtitles(srt)
            if segs:
                return segs, "embedded-subtitles"

    if allow_asr and info.has_audio:
        segs = transcribe(video_path, asr_model)
        if segs is not None:
            return segs, f"whisper:{asr_model}"
        print(
            "vidlayer: no transcript found and no speech-to-text backend installed "
            "(pip install 'vidlayer[whisper]'); continuing with visuals only.",
            file=sys.stderr,
        )
    return [], "none"
