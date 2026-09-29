"""Dynamic obstacle recovery layer.

This module defines dynamic-blocking policy and timeout semantics.
The active Navigator recovery path returns to the previous task point after
a sustained block; original task-point names and mission planning remain unchanged.
"""

from dataclasses import dataclass
import time


@dataclass
class DynamicObstaclePolicy:
    wait_seconds: float = 20.0
    retry_interval: float = 1.0


class DynamicObstacleTimeout(TimeoutError):
    """Dynamic obstacle blocked navigation for too long."""


class DynamicObstacleManager:
    def __init__(self, policy=None):
        self.policy = policy or DynamicObstaclePolicy()

    def wait_until_clear(self, blocked_callback):
        start = time.monotonic()

        while blocked_callback():
            if time.monotonic() - start >= self.policy.wait_seconds:
                raise DynamicObstacleTimeout(
                    "Dynamic obstacle timeout after %.1fs"
                    % self.policy.wait_seconds
                )

            time.sleep(self.policy.retry_interval)
