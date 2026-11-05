import io
import json
import subprocess

import pytest
from PIL import Image, ImageDraw

from vidlayer import VideoDocument, to_anthropic, to_gemini, to_openai
from vidlayer import ffmpeg
from vidlayer.frames import format_ts, frames_at, plan_times, select_keyframes, storyboard
from vidlayer.transcript import load_transcript, parse_subtitles


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


def test_slide_changes_are_scene_changes(tmp_path):
    """Slides share a background, so these changes score far below a hard cut."""
    inputs = []
    for i, box in enumerate([(60, 60, 300, 130), (60, 180, 300, 250), (60, 300, 300, 370)]):
        im = Image.new("RGB", (640, 480), "#10131c")
        ImageDraw.Draw(im).rectangle(box, fill="#f2f4f8")
        im.save(tmp_path / f"{i}.png")
        inputs += ["-loop", "1", "-t", "4", "-r", "10", "-i", str(tmp_path / f"{i}.png")]
    video = tmp_path / "slides.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", *inputs, "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1,format=yuv420p[v]",
         "-map", "[v]", str(video)], check=True,
    )
    assert ffmpeg.scene_changes(video, threshold=0.3) == []  # the old default saw nothing
    doc = VideoDocument.build(video, max_frames=8, asr=False)
    cuts = [f.t for f in doc.frames if f.reason == "scene-change"]
    assert len(cuts) == 2 and abs(cuts[0] - 4.4) < 0.3 and abs(cuts[1] - 8.4) < 0.3, cuts


def test_weak_cuts_are_deduped_but_strong_cuts_stay(tmp_path, monkeypatch):
    video = tmp_path / "static.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=gray:s=320x240:d=10:r=10",
         "-pix_fmt", "yuv420p", str(video)], check=True,
    )
    info = ffmpeg.probe(video)

    def reasons(score):
        monkeypatch.setattr(ffmpeg, "scene_changes", lambda *a, **k: [(5.0, score)])
        return [f.reason for f in select_keyframes(video, info, max_frames=8)]

    assert reasons(0.05) == ["start"]  # false cut on an unchanged picture
    assert reasons(0.5) == ["start", "scene-change"]


def test_format_ts_carries_when_rounding():
    assert format_ts(59.96) == "01:00.0"
    assert format_ts(1.96) == "00:02.0"
    assert format_ts(3599.96) == "1:00:00.0"
    assert format_ts(12.34) == "00:12.3"


def test_dedupe_keeps_repeats_that_are_not_rolling():
    srt = (
        "1\n00:00:01,000 --> 00:00:02,000\nDo you know the answer?\n\n"
        "2\n00:05:00,000 --> 00:05:01,000\nknow\n"
    )
    assert [s.text for s in parse_subtitles(srt)] == ["Do you know the answer?", "know"]


def test_sidecar_matching(tmp_path):
    srt = "1\n00:00:00,000 --> 00:00:01,000\n{}\n"
    info = ffmpeg.VideoInfo("", 1.0, 1, 1, 1.0, has_audio=False, subtitle_streams=0)
    # A different video's subtitles must not be picked up...
    (tmp_path / "talk2.srt").write_text(srt.format("other video"))
    assert load_transcript(tmp_path / "talk.mp4", info=info) == ([], "none")
    # ...while language suffixes and glob characters in the name still match.
    (tmp_path / "clip [1080p].en.vtt").write_text(srt.format("mine"))
    segs, source = load_transcript(tmp_path / "clip [1080p].mp4", info=info)
    assert [s.text for s in segs] == ["mine"] and source == "sidecar:clip [1080p].en.vtt"


def test_frame_at_very_end_decodes(sample_video):
    # The clip's last frame is at 11.9s, so seeking to 11.95s decodes nothing.
    assert frames_at(sample_video, [11.95])[0].jpeg


def test_mcp_reopen_applies_new_arguments(sample_video, tmp_path):
    pytest.importorskip("mcp")
    from vidlayer import mcp_server

    video = tmp_path / "video.mp4"
    video.write_bytes(sample_video.read_bytes())
    subs = tmp_path / "elsewhere.srt"
    subs.write_text("1\n00:00:00,500 --> 00:00:02,000\nSupplied line.\n")

    assert "Supplied line." not in mcp_server.open_video(str(video))[0]
    assert "Supplied line." in mcp_server.open_video(str(video), transcript_path=str(subs))[0]
    # Omitting it later keeps the transcript the user supplied.
    assert "Supplied line." in mcp_server.open_video(str(video))[0]
    assert "Keyframes: 2." in mcp_server.open_video(str(video), max_frames=2)[0]

    vid = mcp_server.video_id(video)
    assert mcp_server.get_frame(vid, 9999)[1].data
    assert len(mcp_server.get_frames(vid, 0, 9999, count=2)) >= 4
