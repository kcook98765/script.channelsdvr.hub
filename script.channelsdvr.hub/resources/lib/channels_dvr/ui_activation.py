"""Immutable published button intent; no SDK/window/worker references."""
from dataclasses import dataclass


@dataclass(frozen=True, eq=False)
class ButtonActivation:
    control_id: int
    command: str
    label_key: str
    context: tuple
    # Scalar tuple identity is combined with monotonic/cache-origin epochs.
    # Never retain the collection: its final release belongs to the CPU worker.
    snapshot: tuple | None = None
    channels: int | None = None

    def matches(self, other):
        return (type(other) is ButtonActivation and self.control_id == other.control_id
                and self.command == other.command and self.label_key == other.label_key
                and self.context == other.context and self.snapshot == other.snapshot
                and self.channels == other.channels)
