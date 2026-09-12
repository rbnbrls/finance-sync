"""Verification result types; only verification may resolve work."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class VerificationResult:
    resolved: bool
    reason: str
