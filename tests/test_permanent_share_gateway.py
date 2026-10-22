"""Permanent grants keep secure, bounded browser persistence and live revocation."""
from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import patch

import test_share_gateway as gateway_fixture
from fl_dashboard.services.share_gateway import PERMANENT_COOKIE_SECONDS


class PermanentShareGatewayTests(unittest.TestCase):
    setUp = gateway_fixture.GatewayTests.setUp
    tearDown = gateway_fixture.GatewayTests.tearDown
    request = gateway_fixture.GatewayTests.request
    login = gateway_fixture.GatewayTests.login

    def test_permanent_link_can_be_redeemed_later_with_a_bounded_secure_cookie(self):
        share = self.store.create("Permanent fixture", permanent=True)
        future = datetime.now(timezone.utc) + timedelta(days=3650)
        with patch("fl_dashboard.services.access_shares._utc_now", return_value=future):
            status, headers, raw = self.login(share["token"])
            self.assertEqual(status, 200)
            data = json.loads(raw)
            self.assertTrue(data["permanent"])
            self.assertIsNone(data["expires_at"])
            self.assertNotIn(share["token"], raw.decode())
            for value in ("Secure", "HttpOnly", "SameSite=Strict", "Path=/",
                          f"Max-Age={PERMANENT_COOKIE_SECONDS}"):
                self.assertIn(value, headers["Set-Cookie"])
            self.assertEqual(PERMANENT_COOKIE_SECONDS, 86400)
            self.assertEqual(200, self.request("/api/codex-sessions", token=share["token"])[0])
            self.assertEqual(200, self.login(share["token"])[0])
        self.store.revoke(share["id"])
        self.assertEqual(401, self.request("/api/codex-sessions", token=share["token"])[0])
        self.assertEqual(401, self.login(share["token"])[0])

    def test_finite_share_keeps_its_expiry_and_cannot_create_other_grants(self):
        finite = self.store.create(expires_in_hours=1)
        status, headers, raw = self.login(finite["token"])
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(raw)["permanent"])
        age = int(headers["Set-Cookie"].split("Max-Age=", 1)[1].split(";", 1)[0])
        self.assertTrue(0 < age <= 3600)
        permanent = self.store.create(permanent=True)
        self.assertEqual(403, self.request("/api/access-shares", method="POST",
                                         body={"permanent": True}, token=permanent["token"])[0])
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
