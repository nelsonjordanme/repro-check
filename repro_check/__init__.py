"""repro-check — a runnability scaffold for reproducing computational papers."""
from .engine import (attempt_executability, reproduce, build_handoff,
                     render_handoff_md, classify_failure, discover_entrypoint)
__version__ = "0.4.1"
__all__ = ["attempt_executability", "reproduce", "build_handoff",
           "render_handoff_md", "classify_failure", "discover_entrypoint"]
