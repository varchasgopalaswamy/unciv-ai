"""Control failures identify the operation and transport cause without exposing secrets."""

import io
import ssl
import unittest
import urllib.error
from email.message import Message
from unittest.mock import Mock, patch

from civ_agent.scheduler import Agent, JsonEndpoint, Operator, OperatorError


class ControlDiagnosticsTest(unittest.TestCase):
    secret = "private-credential-do-not-log"
    url = "https://private-host.invalid/private-path-do-not-log"

    def test_transport_failure_names_operation_role_and_underlying_class_only(self):
        with patch("civ_agent.scheduler.operator_token", return_value=self.secret):
            agent = Agent({"civilizationId": "Greece", "url": self.url, "tokenFile": "unused"})
        operator = Operator(self.url, self.secret, None)
        cases = (
            (operator, "status", TimeoutError(self.secret), "host operator status (TimeoutError)"),
            (
                agent,
                "heartbeat",
                urllib.error.URLError(ConnectionRefusedError(self.secret)),
                "agent service heartbeat (ConnectionRefusedError)",
            ),
            (
                operator,
                "activate",
                urllib.error.URLError(ssl.SSLCertVerificationError(self.secret)),
                "host operator activate (SSLCertVerificationError)",
            ),
            (agent, "interrupt", urllib.error.URLError(self.secret), "agent service interrupt (URLError)"),
        )
        for endpoint, operation, failure, expected in cases:
            with self.subTest(operation=operation):
                endpoint.opener = Mock()
                endpoint.opener.open.side_effect = failure
                with self.assertRaises(OperatorError) as caught:
                    endpoint.call(operation, activationId="private-activation-do-not-log")
                self.assertIn(expected, str(caught.exception))
                self.assertNotIn(self.secret, str(caught.exception))
                self.assertNotIn("private-", str(caught.exception))
                endpoint.opener.open.assert_called_once()  # Diagnostics must not retry mutations.

    def test_invalid_json_identifies_operation_without_echoing_response(self):
        endpoint = JsonEndpoint(self.url, self.secret, None)
        endpoint.opener = Mock()
        endpoint.opener.open.return_value = io.BytesIO(self.secret.encode())
        with self.assertRaises(OperatorError) as caught:
            endpoint.call("events")
        self.assertIn("control endpoint events response (JSONDecodeError)", str(caught.exception))
        self.assertNotIn("private-", str(caught.exception))

    def test_http_rejection_preserves_status_and_code_without_url_or_body(self):
        endpoint = Operator(self.url, self.secret, None)
        endpoint.opener = Mock()
        body = io.BytesIO(b'{"error":"LEASE_LOST","detail":"private-response-do-not-log"}')
        endpoint.opener.open.side_effect = urllib.error.HTTPError(self.url, 409, self.secret, Message(), body)
        with self.assertRaises(OperatorError) as caught:
            endpoint.call("heartbeat")
        self.assertIn("host operator heartbeat rejected: HTTP 409 LEASE_LOST", str(caught.exception))
        self.assertEqual((caught.exception.code, caught.exception.status), ("LEASE_LOST", 409))
        self.assertNotIn("private-", str(caught.exception))
