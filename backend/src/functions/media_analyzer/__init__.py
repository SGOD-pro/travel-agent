"""Media Analyzer module conforming to Phase 10."""

from functions.media_analyzer.handler import analyze_media_handler
from functions.media_analyzer.service import MediaAnalyzerService

__all__ = [
    "MediaAnalyzerService",
    "analyze_media_handler",
]