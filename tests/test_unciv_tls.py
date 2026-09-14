"""Real local certificates and TLS handshakes; no network services or credentials outside temp state."""

import json
import shutil
import socket
import ssl
import stat
import subprocess
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import ExtendedKeyUsageOID

import civ_agent.tls as tls


@contextmanager
def tls_listener(material):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(material["cert_file"], material["key_file"])
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(5)

        def serve():
            try:
                connection, _ = listener.accept()
                with connection:
                    try:
                        with context.wrap_socket(connection, server_side=True):
                            pass
                    except ssl.SSLError:
                        pass
            except OSError:
                pass

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            yield listener.getsockname()[1]
        finally:
            thread.join(timeout=6)
            if thread.is_alive():
                raise AssertionError("TLS test listener did not finish")


class CertificateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="TLS helper tests ")
        cls.root = Path(cls.directory.name)
        cls.material = tls.ensure_server_certificate(cls.root, "test-server")

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_generated_certificates_have_loopback_sans_and_verify_with_the_issuer(self):
        for hostname in ("localhost", "127.0.0.1"):
            with tls_listener(self.material) as port:
                tls.verify_https_connection(f"https://{hostname}:{port}/mcp", self.material["ca_file"])
        for file in (self.root / "tls").rglob("*"):
            expected = 0o700 if file.is_dir() else 0o600
            self.assertEqual(expected, stat.S_IMODE(file.stat().st_mode), file)

    def test_wrong_or_missing_ca_and_wrong_hostname_are_rejected(self):
        with tls_listener(self.material) as port:
            with self.assertRaises(ssl.SSLError):
                tls.verify_https_connection(f"https://localhost:{port}/mcp")
        context = tls.client_ssl_context(self.material["ca_file"])
        with tls_listener(self.material) as port:
            with socket.create_connection(("127.0.0.1", port), timeout=3) as connection:
                with self.assertRaises(ssl.CertificateError):
                    context.wrap_socket(connection, server_hostname="different.example.com")

    def test_existing_certificates_are_reused_without_mutation(self):
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in self.material.values()}
        self.assertEqual(self.material, tls.ensure_server_certificate(self.root, "test-server"))
        self.assertEqual(
            before, {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in self.material.values()}
        )
        with self.assertRaises(tls.TLSConfigError):
            tls.ensure_server_certificate(self.root, "test-server", ["other.example.com"])
        self.assertEqual(
            before, {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in self.material.values()}
        )

    def test_two_service_certificates_share_the_ca_but_have_distinct_keys(self):
        second = tls.ensure_server_certificate(self.root, "agent-Greece")
        self.assertEqual(second["ca_file"], self.material["ca_file"])
        self.assertNotEqual(second["key_file"].read_bytes(), self.material["key_file"].read_bytes())
        with tls_listener(second) as port:
            tls.verify_https_connection(f"https://localhost:{port}/agent", self.material["ca_file"])

    def test_concurrent_provisioning_returns_one_identity(self):
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: tls.ensure_server_certificate(self.root, "concurrent"), range(2)))
        self.assertEqual(results[0], results[1])

    def test_pkcs12_password_stays_in_private_file_and_material_is_stable(self):
        with patch.object(subprocess, "run", side_effect=AssertionError("TLS must not invoke an external tool")) as run:
            config, ca = tls.ensure_host_tls(self.root)
        settings = json.loads(config.read_text())
        password = Path(settings["keyStorePasswordFile"]).read_text().strip()
        self.assertEqual("host", settings["keyAlias"])
        self.assertEqual("127.0.0.1", settings["listenHost"])
        self.assertEqual("localhost", settings["publicHost"])
        self.assertEqual(self.material["ca_file"], ca)
        self.assertEqual(settings, tls.validate_host_tls_config(config))
        run.assert_not_called()
        loaded = pkcs12.load_pkcs12(Path(settings["keyStorePath"]).read_bytes(), password.encode())
        self.assertIsNotNone(loaded.key)
        assert loaded.cert is not None
        self.assertEqual(b"host", loaded.cert.friendly_name)
        self.assertEqual(1, len(loaded.additional_certs))
        path = Path(settings["keyStorePath"])
        before = path.read_bytes()
        self.assertEqual((config, ca), tls.ensure_host_tls(self.root))
        self.assertEqual(before, path.read_bytes())

    def test_codex_trust_keeps_existing_custom_ca_and_adds_game_ca_without_printable_secrets(self):
        workspace = self.root / "codex workspace"
        environment = {"UNCIV_PLAYER_TOKEN": "A" * 43, "CODEX_CA_CERTIFICATE": str(self.material["ca_file"])}
        before = environment.copy()
        updated = tls.codex_trust_environment(self.material["ca_file"], environment, workspace)
        bundle = Path(updated["CODEX_CA_CERTIFICATE"])
        self.assertEqual(before, environment)
        self.assertEqual(environment["UNCIV_PLAYER_TOKEN"], updated["UNCIV_PLAYER_TOKEN"])
        self.assertNotIn("A" * 43, bundle.read_text())
        self.assertEqual(0o600, stat.S_IMODE(bundle.stat().st_mode))
        context = ssl.create_default_context(cafile=str(bundle))
        self.assertGreater(len(context.get_ca_certs()), 1)
        with tls_listener(self.material) as port:
            tls.verify_https_connection(f"https://localhost:{port}/mcp", bundle)

    def test_invalid_validity_and_server_purpose_are_rejected_without_replacement(self):
        cert = x509.load_pem_x509_certificate(self.material["cert_file"].read_bytes())
        key = serialization.load_pem_private_key((self.root / "tls/ca/key.pem").read_bytes(), password=None)
        assert isinstance(key, rsa.RSAPrivateKey)
        now = datetime.now(UTC)
        for failure in ("expired", "future", "client-only"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                shutil.copytree(self.root / "tls", root / "tls")
                before = now + timedelta(days=2) if failure == "future" else now - timedelta(days=2)
                after = now - timedelta(days=1) if failure == "expired" else now + timedelta(days=5)
                builder = (
                    x509.CertificateBuilder()
                    .subject_name(cert.subject)
                    .issuer_name(cert.issuer)
                    .public_key(cert.public_key())
                    .serial_number(x509.random_serial_number())
                    .not_valid_before(before)
                    .not_valid_after(after)
                )
                for extension in cert.extensions:
                    value = extension.value
                    if failure == "client-only" and isinstance(value, x509.ExtendedKeyUsage):
                        value = x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH])
                    builder = builder.add_extension(value, critical=extension.critical)
                invalid = builder.sign(key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM)
                path = root / "tls/test-server/cert.pem"
                path.write_bytes(invalid)
                with self.assertRaises(tls.TLSConfigError):
                    tls.ensure_server_certificate(root, "test-server")
                self.assertEqual(invalid, path.read_bytes())

    @unittest.skipUnless(shutil.which("java"), "JDK is required for PKCS12 interoperability")
    def test_generated_keystore_loads_in_the_jvm_without_exposing_the_password(self):
        config, _ = tls.ensure_host_tls(self.root)
        settings = json.loads(config.read_text())
        source = self.root / "VerifyKeyStore.java"
        source.write_text("""
import java.nio.file.*;
import java.security.*;
import java.security.cert.X509Certificate;
import javax.net.ssl.*;
class VerifyKeyStore {
    public static void main(String[] args) throws Exception {
        char[] password = Files.readString(Path.of(args[1])).strip().toCharArray();
        KeyStore store = KeyStore.getInstance("PKCS12");
        try (var input = Files.newInputStream(Path.of(args[0]))) { store.load(input, password); }
        if (!(store.getKey("host", password) instanceof PrivateKey)) throw new AssertionError();
        if (store.getCertificateChain("host").length != 2) throw new AssertionError();
        ((X509Certificate) store.getCertificate("host")).checkValidity();
        KeyManagerFactory keys = KeyManagerFactory.getInstance(KeyManagerFactory.getDefaultAlgorithm());
        keys.init(store, password);
        SSLContext context = SSLContext.getInstance("TLS");
        context.init(keys.getKeyManagers(), null, null);
        System.out.println("JVM TLS identity loaded");
    }
}
""")
        output = subprocess.run(
            ["java", str(source), settings["keyStorePath"], settings["keyStorePasswordFile"]],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        self.assertEqual("JVM TLS identity loaded\n", output.stdout)
        self.assertNotIn(Path(settings["keyStorePasswordFile"]).read_text().strip(), output.stderr)

    def test_incomplete_or_insecure_private_material_is_not_replaced(self):
        with tempfile.TemporaryDirectory(prefix="invalid TLS state ") as directory:
            root = Path(directory)
            ca = root / "tls/ca"
            ca.mkdir(mode=0o700, parents=True)
            (root / "tls").chmod(0o700)
            key = ca / "key.pem"
            key.write_text("DO-NOT-REPLACE-PRIVATE-MATERIAL")
            key.chmod(0o600)
            with self.assertRaises(tls.TLSConfigError):
                tls.ensure_server_certificate(root, "host")
            self.assertEqual("DO-NOT-REPLACE-PRIVATE-MATERIAL", key.read_text())
        key = self.material["key_file"]
        original = key.read_bytes()
        key.chmod(0o644)
        try:
            with self.assertRaises(tls.TLSConfigError):
                tls.ensure_server_certificate(self.root, "test-server")
            self.assertEqual(original, key.read_bytes())
        finally:
            key.chmod(0o600)

    def test_urls_reject_plaintext_credentials_and_ambiguous_paths(self):
        for value in (
            "http://localhost",
            "https://a:b@localhost",
            "https://localhost?token=secret",
            "https://localhost/#fragment",
            "https://localhost:0",
            "https://localhost:bad",
            "https://localhost/other",
            "https://localhost\n",
            "https://localhost\\@example.com",
        ):
            with self.subTest(value=value), self.assertRaises(tls.TLSConfigError):
                tls.https_url(value)
        self.assertEqual("https://localhost:8765", tls.https_url("https://localhost:8765/"))

    def test_endpoint_override_and_malformed_config_errors_never_quote_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tls.write_private_json(
                root / "endpoints.json", {"hostUrl": "https://localhost:8765", "caFile": str(self.material["ca_file"])}
            )
            self.assertEqual(("https://localhost:8765", self.material["ca_file"]), tls.endpoint_configuration(root))
            self.assertEqual("https://localhost:9999", tls.endpoint_configuration(root, port=9999)[0])
            with self.assertRaises(tls.TLSConfigError):
                tls.endpoint_configuration(root, "https://example.com", port=9999)
            (root / "endpoints.json").write_text('{"DO-NOT-QUOTE-ME":')
            with self.assertRaises(tls.TLSConfigError) as error:
                tls.endpoint_configuration(root)
            self.assertNotIn("DO-NOT-QUOTE-ME", str(error.exception))
