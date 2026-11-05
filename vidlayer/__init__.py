"""vidlayer: a video compatibility layer for LLMs."""

from .document import VideoDocument
from .render import to_anthropic, to_gemini, to_markdown, to_openai

__all__ = ["VideoDocument", "to_anthropic", "to_openai", "to_gemini", "to_markdown"]
__version__ = "0.1.1"
