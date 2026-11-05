"""Provider adapters: VideoDocument -> message content for a specific LLM API.

Every adapter walks the same `doc.timeline()`, so adding a new provider means
writing ~20 lines that map "text" and "image" to that API's block types.
"""

from __future__ import annotations

from typing import Any, Callable

from .document import VideoDocument, b64
from .frames import Frame, format_ts


def _walk(doc: VideoDocument, text: Callable[[str], Any], image: Callable[[Frame], Any]) -> list[Any]:
    """Emit [text, image, text, image, ...], merging consecutive text into one block."""
    blocks: list[Any] = []
    pending: list[str] = [doc.header()]

    def flush() -> None:
        if pending:
            blocks.append(text("\n".join(pending)))
            pending.clear()

    for item in doc.timeline():
        if isinstance(item, Frame):
            pending.append(f"[{format_ts(item.t)}] keyframe ({item.reason}):")
            flush()
            blocks.append(image(item))
        else:
            pending.append("Transcript:\n" + doc.format_segments(item))
    pending.append(doc.footer)
    flush()
    return blocks


# ---- Anthropic (Claude) ---------------------------------------------------
def to_anthropic(doc: VideoDocument) -> list[dict]:
    """Content blocks for the Claude Messages API (`messages[i].content`)."""
    return _walk(
        doc,
        lambda s: {"type": "text", "text": s},
        lambda f: {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64(f.jpeg)}},
    )


# ---- OpenAI-compatible (OpenAI, OpenRouter, vLLM, Ollama, LM Studio, ...) ---
def to_openai(doc: VideoDocument, detail: str = "auto") -> list[dict]:
    """Content parts for OpenAI-style Chat Completions (`messages[i].content`)."""
    return _walk(
        doc,
        lambda s: {"type": "text", "text": s},
        lambda f: {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64(f.jpeg)}", "detail": detail}},
    )


# ---- Google Gemini ----------------------------------------------------------
def to_gemini(doc: VideoDocument) -> list[dict]:
    """`parts` for a Gemini `contents` entry (REST shape)."""
    return _walk(
        doc,
        lambda s: {"text": s},
        lambda f: {"inline_data": {"mime_type": "image/jpeg", "data": b64(f.jpeg)}},
    )


# ---- Markdown (for agents that read files, or for humans) -----------------
def to_markdown(doc: VideoDocument) -> str:
    """Markdown referencing frames/*.jpg as written by VideoDocument.save()."""
    out = ["```", doc.header(), "```", ""]
    for item in doc.timeline():
        if isinstance(item, Frame):
            out.append(f"### [{format_ts(item.t)}] keyframe ({item.reason})")
            out.append(f"![frame at {format_ts(item.t)}](frames/{item.t:09.2f}.jpg)")
        else:
            out.append("> " + doc.format_segments(item).replace("\n", "\n> "))
        out.append("")
    return "\n".join(out)


ADAPTERS = {
    "anthropic": to_anthropic,
    "claude": to_anthropic,
    "openai": to_openai,
    "gemini": to_gemini,
}
