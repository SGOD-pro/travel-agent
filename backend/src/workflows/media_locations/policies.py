"""Policies for the Media Location Workflow."""
from .state import MediaLocationState

def should_verify_again(state: MediaLocationState) -> str:
    """Decides whether to loop back to extraction or end the workflow."""
    if state.get("attempt_count", 0) < 3:
        return "extract_media"
    return "end"