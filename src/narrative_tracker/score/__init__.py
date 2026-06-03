"""Feedback-loop scoring (M4): credibility + attribution."""

from .credibility import attribute_call, recompute_credibility

__all__ = ["recompute_credibility", "attribute_call"]
