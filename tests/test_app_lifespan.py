from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src import __main__ as app_module


class AppLifespanTests(unittest.TestCase):
    def test_static_mode_starts_without_playwright(self):
        app = SimpleNamespace(state=SimpleNamespace())

        async def run_lifespan():
            with (
                patch.object(app_module, "init_db"),
                patch.object(app_module, "ensure_bucket"),
                patch.object(app_module, "cleanup_stale_temporary_media"),
                patch.object(app_module, "TRAVEL_ENABLE_PLAYWRIGHT_FALLBACK", False),
            ):
                async with app_module.lifespan(app):
                    self.assertIsNone(app.state.browser)
                    self.assertIsNone(app.state.playwright)

        asyncio.run(run_lifespan())


if __name__ == "__main__":
    unittest.main()
