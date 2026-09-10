"""
Status tracking module for ECHELON compilation lifecycle.

Law: Status events are strictly append-only. The 'latest_status' function
resolves to the most recent event recorded for a given intent_id, adhering
to the "latest wins at read" principle.
"""
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, List
import time

class Status(str, Enum):
    """Represents the discrete states of an ECHELON compilation intent."""
    DONE = "DONE"
    DONE_WITH_CONCERNS = "DONE_WITH_CONCERNS"
    NEEDS_CONTEXT = "NEEDS_CONTEXT"
    BLOCKED = "BLOCKED"
    SPEC_APPROVED = "SPEC_APPROVED"
    QUALITY_APPROVED = "QUALITY_APPROVED"
    REJECTED = "REJECTED"

@dataclass(frozen=True)
class StatusEvent:
    """A timestamped, immutable record of a status change for an intent."""
    intent_id: str
    status: Status
    stage: Optional[str] = None
    note: str = ''
    at: float = field(default_factory=time.time)

def latest_status(events: List[StatusEvent], intent_id: str) -> Optional[StatusEvent]:
    """
    Finds and returns the most recent StatusEvent for a given intent ID.

    Args:
        events: A list of all recorded StatusEvents (assumed to be chronological, 
                though sorting handles out-of-order arrival).
        intent_id: The unique identifier of the compilation intent.

    Returns:
        The latest StatusEvent for the ID, or None if no events are found.
    """
    relevant_events = [event for event in events if event.intent_id == intent_id]
    if not relevant_events:
        return None
    
    # Sort by 'at' timestamp; the last element is the latest.
    # Since we are using dataclasses, comparison works naturally on fields.
    relevant_events.sort(key=lambda e: e.at)
    return relevant_events[-1]

def is_compile_eligible(events: List[StatusEvent], intent_id: str) -> bool:
    """
    Checks if an intent is ready to proceed to the compilation phase.

    Eligibility requires that the latest recorded status for the intent is 
    specifically QUALITY_APPROVED.

    Args:
        events: A list of all recorded StatusEvents.
        intent_id: The unique identifier of the compilation intent.

    Returns:
        True if the latest status is QUALITY_APPROVED, False otherwise.
    """
    latest = latest_status(events, intent_id)
    if latest is None:
        # If no events exist, it's not yet eligible (defaulting to False/Needs Context state)
        return False
    
    return latest.status == Status.QUALITY_APPROVED
