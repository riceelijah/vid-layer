"""Who is speaking when, from the audio. Needs the `diarize` extra; models download on first use."""

from __future__ import annotations

import hashlib
import os
import sys
import tarfile
import tempfile
import urllib.request
import wave
from pathlib import Path

from . import ffmpeg
from .transcript import Segment

_RELEASES = "https://github.com/k2-fsa/sherpa-onnx/releases/download"
_SEGMENTATION = (
    f"{_RELEASES}/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2",
    "24615ee884c897d9d2ba09bb4d30da6bb1b15e685065962db5b02e76e4996488",
)
_EMBEDDING = (
    f"{_RELEASES}/speaker-recongition-models/wespeaker_en_voxceleb_resnet34_LM.onnx",  # "recongition" is their spelling
    "e9848563da86f263117134dfd7ad63c92355b37de492b55e325400c9d9c39012",
)


def _download(url: str, sha256: str) -> Path:
    """Fetch a file into the cache once and check it against its pinned hash."""
    target = Path.home() / ".cache" / "vidlayer" / "models" / url.rsplit("/", 1)[1]
    if target.exists():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    print(f"vidlayer: downloading {target.name}...", file=sys.stderr)
    with urllib.request.urlopen(url, timeout=60) as response:
        data = response.read()
    if hashlib.sha256(data).hexdigest() != sha256:
        raise ValueError(f"Checksum mismatch for {url}; not using it.")
    _write(target, data)
    return target


def _write(target: Path, data: bytes) -> None:
    """All or nothing, so an interrupted run can't leave a broken model in the cache."""
    tmp = target.with_suffix(".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, target)


def _segmentation_model() -> Path:
    archive = _download(*_SEGMENTATION)
    model = archive.with_name("pyannote-segmentation-3-0.onnx")
    if not model.exists():
        with tarfile.open(archive) as tar:
            _write(model, tar.extractfile("sherpa-onnx-pyannote-segmentation-3-0/model.onnx").read())
    return model


def speaker_turns(path: str | Path, num_speakers: int | None = None) -> list[tuple[float, float, int]]:
    """Return [(start, end, speaker)] sorted by start. Guesses the speaker count unless told."""
    try:
        import numpy as np
        import sherpa_onnx
    except ImportError:
        raise ValueError("Speaker diarization needs sherpa-onnx: pip install 'vidlayer[diarize]'") from None

    threads = os.cpu_count() or 1
    config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(_segmentation_model())),
            num_threads=threads,
        ),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(_download(*_EMBEDDING)), num_threads=threads),
        clustering=sherpa_onnx.FastClusteringConfig(num_clusters=num_speakers or -1, threshold=0.5),
        min_duration_on=0.3,
        min_duration_off=0.5,
    )
    with tempfile.TemporaryDirectory() as tmp:
        with wave.open(str(ffmpeg.extract_audio_wav(path, Path(tmp) / "audio.wav"))) as w:
            pcm = w.readframes(w.getnframes())
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768
    turns = sherpa_onnx.OfflineSpeakerDiarization(config).process(samples).sort_by_start_time()
    return [(t.start, t.end, t.speaker) for t in turns]


def assign_speakers(segments: list[Segment], turns: list[tuple[float, float, int]]) -> None:
    """Label each segment with whoever talks most during it, numbered by first appearance."""
    # ponytail: one label per segment, so a caption spanning a speaker change goes to the
    # majority. Split on word timestamps if Whisper segments turn out too long for this.
    names: dict[int, str] = {}
    for seg in segments:
        talk: dict[int, float] = {}
        for start, end, speaker in turns:
            overlap = min(seg.end, end) - max(seg.start, start)
            if overlap > 0:
                talk[speaker] = talk.get(speaker, 0.0) + overlap
        if talk:
            who = max(talk, key=talk.get)
            seg.speaker = names.setdefault(who, f"Speaker {len(names) + 1}")
