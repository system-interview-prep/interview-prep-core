"""Shared SQL fragments used by admin reporting routes."""

EFFECTIVE_SESSION_MODE = "LOWER(COALESCE(s.mode, s.type, 'text'))"
