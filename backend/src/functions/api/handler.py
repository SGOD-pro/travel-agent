"""ASGI entrypoint for deployment adapters and local server runners."""

from functions.api.app import app

handler = app

__all__ = ["app", "handler"]
