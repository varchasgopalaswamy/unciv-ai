"""Control failures identify the operation and transport cause without exposing secrets."""

import io
import json
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
                self.assertEqual(endpoint.opener.open.call_count, 2 if operation == "status" else 1)

    def test_control_timeout_is_fifteen_seconds_and_mutations_are_never_retried(self):
        for cls in (JsonEndpoint, Operator):
            endpoint = cls(self.url, self.secret, None)
            endpoint.opener = Mock()
            endpoint.opener.open.side_effect = TimeoutError()
            with self.assertRaises(OperatorError):
                endpoint.call("activate")
            endpoint.opener.open.assert_called_once()
            self.assertEqual(endpoint.opener.open.call_args.kwargs["timeout"], 15)

    def test_status_retries_a_lost_response_once(self):
        endpoint = Operator(self.url, self.secret, None)
        endpoint.opener = Mock()
        response = {"status": {"epoch": "e", "serverInstance": "s", "phase": "ACTIVE", "turn": 1, "handoff": 3}}
        endpoint.opener.open.side_effect = [TimeoutError(), io.BytesIO(json.dumps(response).encode())]
        self.assertEqual(endpoint.call("status"), response)
        self.assertEqual(endpoint.opener.open.call_count, 2)

    def test_retry_uses_only_remaining_confirmed_lease_time(self):
        endpoint = Operator(self.url, self.secret, None)
        endpoint._lease_deadline = 130.0
        endpoint.opener = Mock()
        clock = [100.0]

        def timeout(request, *, timeout):
            clock[0] += timeout
            raise TimeoutError()

        endpoint.opener.open.side_effect = timeout
        with patch("civ_agent.scheduler.time.monotonic", side_effect=lambda: clock[0]):
            with self.assertRaises(OperatorError):
                endpoint.call("heartbeat", leaseId="lease")
        self.assertEqual([call.kwargs["timeout"] for call in endpoint.opener.open.call_args_list], [15, 10])
        self.assertEqual(clock[0], 125.0)

    def test_no_retry_after_lease_margin_is_reached(self):
        endpoint = Operator(self.url, self.secret, None)
        endpoint._lease_deadline = 120.0
        endpoint.opener = Mock()
        endpoint.opener.open.side_effect = TimeoutError()
        with patch("civ_agent.scheduler.time.monotonic", side_effect=[100.0, 115.0]):
            with self.assertRaises(OperatorError):
                endpoint.call("heartbeat", leaseId="lease")
        endpoint.opener.open.assert_called_once()

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
