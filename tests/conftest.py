import subprocess
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def sample_video(tmp_path_factory) -> Path:
    """12s video with 3 hard cuts (red, green, testsrc, blue) + tone audio + a sidecar SRT."""
    d = tmp_path_factory.mktemp("vid")
    out = d / "sample.mp4"
    srcs = ["color=c=red:", "color=c=white:", "testsrc=", "color=c=blue:"]
    inputs = []
    for s in srcs:
        inputs += ["-f", "lavfi", "-i", f"{s}s=320x240:d=3:r=10"]
    inputs += ["-f", "lavfi", "-i", "sine=f=440:d=12"]
    fc = "".join(f"[{i}:v]" for i in range(4)) + "concat=n=4:v=1:a=0[v]"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", *inputs, "-filter_complex", fc,
         "-map", "[v]", "-map", "4:a", "-pix_fmt", "yuv420p", "-shortest", str(out)],
        check=True,
    )
    (d / "sample.srt").write_text(
        "1\n00:00:00,500 --> 00:00:02,000\nThis is the red part.\n\n"
        "2\n00:00:04,000 --> 00:00:05,500\nNow it turns <i>white</i>.\n\n"
        "3\n00:00:10,000 --> 00:00:11,500\nAnd finally blue.\n"
    )
    return out
