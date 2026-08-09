from __future__ import annotations

from .webapp import create_app

app = create_app()

__all__ = ["app", "create_app"]
