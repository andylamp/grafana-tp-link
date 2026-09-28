# Copyright (c) 2026 grafana-tp-link contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Expose the isolated stack fixture to opt-in integration tests."""

from tests.integration.support import stack

__all__ = ["stack"]
