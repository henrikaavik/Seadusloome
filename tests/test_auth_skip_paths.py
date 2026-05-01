"""SKIP_PATHS lets unauthenticated users reach forgot + reset pages."""

from __future__ import annotations

import re

from app.auth.middleware import SKIP_PATHS


def test_forgot_in_skip_paths():
    assert any(re.fullmatch(p, "/auth/forgot") for p in SKIP_PATHS)


def test_reset_with_token_in_skip_paths():
    assert any(re.fullmatch(p, "/auth/reset/abc123") for p in SKIP_PATHS)
