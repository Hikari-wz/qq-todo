"""Optional comma-separated QQ group IDs to mute, loaded at startup."""
import os

MUTED_GROUPS = frozenset(
    group.strip() for group in os.environ.get("TODO_MUTED_GROUPS", "").split(",")
    if group.strip()
)


def muted_message(key):
    parts = key.split(":", 3)
    return len(parts) == 4 and parts[0] == "qq" and parts[2] in MUTED_GROUPS


def muted_card(card):
    return any(muted_message(source["id"]) for source in card.get("evidence", []))
