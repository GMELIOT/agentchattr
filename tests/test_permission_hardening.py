import asyncio
import unittest
from unittest import mock

import app


class FakePendingPermissionStore:
    def get(self, perm_id: str) -> dict:
        return {"id": perm_id, "status": "pending"}


class FakeRequest:
    async def json(self):
        return {}


class PermissionHardeningTests(unittest.TestCase):
    def test_wait_for_permission_resolution_times_out_closed(self):
        old_store = app.permission_store
        app.permission_store = FakePendingPermissionStore()
        self.addCleanup(setattr, app, "permission_store", old_store)

        result = asyncio.run(
            app._wait_for_permission_resolution("perm-1", timeout_seconds=0.01)
        )

        self.assertEqual(
            result,
            {"id": "perm-1", "status": "denied", "reason": "timeout"},
        )

    def test_telegram_callback_uses_constant_time_secret_compare(self):
        old_notifier = app.telegram_notifier
        old_secret = app.telegram_callback_secret
        app.telegram_notifier = object()
        app.telegram_callback_secret = "expected-secret"
        self.addCleanup(setattr, app, "telegram_notifier", old_notifier)
        self.addCleanup(setattr, app, "telegram_callback_secret", old_secret)

        with mock.patch("hmac.compare_digest", return_value=False) as compare:
            response = asyncio.run(
                app.telegram_callback("wrong-secret", FakeRequest())
            )

        compare.assert_called_once_with("wrong-secret", "expected-secret")
        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
