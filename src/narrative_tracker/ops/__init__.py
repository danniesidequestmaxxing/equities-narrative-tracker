"""Operational concerns: dead-man's-switch heartbeat, health state."""

from .heartbeat import ping_heartbeat

__all__ = ["ping_heartbeat"]
