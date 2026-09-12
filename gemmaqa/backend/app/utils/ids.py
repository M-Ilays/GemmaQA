"""UUID helpers for entity identifiers."""

from uuid import UUID, uuid4


def new_id() -> str:
    """Generate a new UUID4 string."""
    return str(uuid4())


def parse_id(value: str) -> UUID:
    """Parse and validate a UUID string."""
    return UUID(value)


def is_valid_id(value: str) -> bool:
    """Return True if value is a valid UUID string."""
    try:
        UUID(value)
        return True
    except (ValueError, TypeError, AttributeError):
        return False
