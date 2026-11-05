import io
import json

from PIL import Image

from vidlayer import VideoDocument, to_anthropic, to_gemini, to_openai
from vidlayer import ffmpeg
from vidlayer.frames import plan_times, storyboard
from vidlayer.transcript import parse_subtitles


def test_probe(sample_video):
    info = ffmpeg.probe(sample_video)
    assert 11.5 < info.duration < 12.5
    assert (info.width, info.height) == (320, 240)
    assert info.has_audio


def test_scene_changes_found(sample_video):
    cuts = [t for t, _ in ffmpeg.scene_changes(sample_video)]
    for expected in (3, 6, 9):
        assert any(abs(t - expected) < 0.5 for t in cuts), cuts


def test_parse_vtt_and_rolling_dedupe():
    vtt = (
        "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nhello\n\n"
        "00:00:02.000 --> 00:00:03.000\nhello world\n\n"
        "00:00:03.000 --> 00:00:04.000\nworld\n"
    )
    segs = parse_subtitles(vtt)
    assert [s.text for s in segs] == ["hello", "world"]
    assert segs[0].start == 1.0


def test_plan_respects_budget():
    cuts = [(float(t), 0.5 + (t % 7) / 10) for t in range(1, 300, 2)]
    plan = plan_times(300, cuts, max_frames=20, max_gap=15)
    assert len(plan) <= 20
    times = [p[0] for p in plan]
    assert times == sorted(times)
    assert times[-1] > 200  # coverage reaches the end


def test_build_document(sample_video):
    doc = VideoDocument.build(sample_video, max_frames=12, asr=False)
    assert doc.transcript_source == "sidecar:sample.srt"
    assert [s.text for s in doc.segments][1] == "Now it turns white."
    # one frame per shot at least
    colors = set()
    for f in doc.frames:
        r, g, b = Image.open(io.BytesIO(f.jpeg)).convert("RGB").resize((1, 1)).getpixel((0, 0))
        colors.add(max(("r", r), ("g", g), ("b", b), key=lambda x: x[1])[0])
    assert {"r", "b"} <= colors and len(doc.frames) >= 4


def test_adapters(sample_video):
    doc = VideoDocument.build(sample_video, max_frames=8, asr=False)
    a = to_anthropic(doc)
    assert a[0]["type"] == "text" and a[0]["text"].startswith("<video")
    assert sum(b["type"] == "image" for b in a) == len(doc.frames)
    assert a[-1]["text"].endswith("</video>")
    # no two adjacent text blocks
    assert not any(x["type"] == y["type"] == "text" for x, y in zip(a, a[1:]))
    assert "white" in json.dumps(a)
    o = to_openai(doc)
    assert sum(b["type"] == "image_url" for b in o) == len(doc.frames)
    g = to_gemini(doc)
    assert sum("inline_data" in b for b in g) == len(doc.frames)


def test_save_load_roundtrip(sample_video, tmp_path):
    doc = VideoDocument.build(sample_video, max_frames=6, asr=False)
    doc.save(tmp_path / "out")
    md = (tmp_path / "out" / "video.md").read_text()
    assert "![frame at" in md
    doc2 = VideoDocument.load(tmp_path / "out")
    assert len(doc2.frames) == len(doc.frames)
    assert doc2.segments == doc.segments


def test_storyboard(sample_video):
    doc = VideoDocument.build(sample_video, max_frames=6, asr=False)
    img = Image.open(io.BytesIO(storyboard(doc.frames, cols=3, tile_width=200)))
    assert img.width == 600
