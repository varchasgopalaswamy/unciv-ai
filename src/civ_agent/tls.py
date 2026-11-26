"""Local certificate provisioning and verified HTTPS configuration shared by launchers."""

import fcntl
import ipaddress
import json
import os
import re
import secrets
import socket
import ssl
import stat
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from cryptography import x509
from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from cryptography.x509.verification import PolicyBuilder, Store, VerificationError


class TLSConfigError(ValueError):
    pass


def private_directory(path):
    path = Path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir() or stat.S_IMODE(path.stat().st_mode) & ~0o700:
        raise TLSConfigError(f"TLS directory must be an owner-only directory (chmod 700): {path}")
    return path


def regular_file(path, private=False):
    path = Path(path)
    if (private and path.is_symlink()) or not path.is_file():
        raise TLSConfigError(f"Expected a regular file: {path}")
    if private and stat.S_IMODE(path.stat().st_mode) & ~0o600:
        raise TLSConfigError(f"Private file must have owner-only permissions (chmod 600): {path}")
    return path


def write_private(path, content):
    """Replace a launcher-owned file atomically without following an existing symlink."""
    path = Path(path)
    if path.exists() or path.is_symlink():
        regular_file(path, private=True)
        if path.read_bytes() == content:
            return
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_private_json(path, value):
    write_private(path, (json.dumps(value, indent=2) + "\n").encode())


def read_json(path):
    regular_file(path)
    try:
        with Path(path).open("rb") as stream:
            data = stream.read(65537)
        if len(data) > 65536:
            raise ValueError()
        return json.loads(data)
    except (ValueError, UnicodeError):
        raise TLSConfigError("Invalid endpoint or TLS configuration file.") from None


def https_url(value, allowed_paths=("", "/")):
    """Reject ambiguous URLs before any credentials or network requests are used."""
    if not isinstance(value, str) or any(ord(char) <= 32 or ord(char) == 127 for char in value):
        raise TLSConfigError("Endpoint must be an HTTPS URL without credentials, query, or fragment.")
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme == "https"
            and parsed.hostname
            and parsed.port != 0
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
            and parsed.path in allowed_paths
            and "\\" not in parsed.netloc
        )
    except ValueError:
        raise TLSConfigError("Endpoint must be an HTTPS URL without credentials, query, or fragment.") from None
    if not valid:
        raise TLSConfigError("Endpoint must be an HTTPS URL without credentials, query, or fragment.")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def host_name(value):
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", value):
            raise TLSConfigError("Public host must be an IP address or DNS hostname.") from None
        if any(
            not label or len(label) > 63 or label.startswith("-") or label.endswith("-") for label in value.split(".")
        ):
            raise TLSConfigError("Public host must be an IP address or DNS hostname.") from None
        return value.lower()


def host_origin(host, port):
    host = host_name(host)
    return f"https://{'[' + host + ']' if ':' in host else host}:{port}"


@contextmanager
def _certificate_errors():
    try:
        yield
    except TLSConfigError:
        raise
    except (ValueError, TypeError, InvalidSignature, UnsupportedAlgorithm, VerificationError, x509.ExtensionNotFound):
        # Library diagnostics can contain private material. Keep errors input-free.
        raise TLSConfigError("Invalid or expired local certificate, key, or key store.") from None


def _key(path):
    regular_file(path, private=True)
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise TLSConfigError("Local certificate keys must be RSA private keys.")
    return key


def _cert(path):
    regular_file(path, private=True)
    cert = x509.load_pem_x509_certificate(path.read_bytes())
    now = datetime.now(UTC)
    if not cert.not_valid_before_utc <= now or cert.not_valid_after_utc <= now + timedelta(days=1):
        raise TLSConfigError("Local certificate is not valid for the next 24 hours.")
    return cert


def _builder(key, subject, issuer, ca, days):
    now = datetime.now(UTC)
    return (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=not ca,
                content_commitment=False,
                key_encipherment=not ca,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=ca,
                crl_sign=ca,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
    )


def _write_identity(directory, key, cert):
    write_private(
        directory / "key.pem",
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()),
    )
    write_private(directory / "cert.pem", cert.public_bytes(serialization.Encoding.PEM))


def _subject(host):
    try:
        return x509.IPAddress(ipaddress.ip_address(host))
    except ValueError:
        return x509.DNSName(host)


@contextmanager
def _certificate_lock(state_dir):
    directory = private_directory(Path(state_dir).resolve() / "tls")
    descriptor = os.open(directory / ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if stat.S_IMODE(os.fstat(descriptor).st_mode) & ~0o600:
            raise TLSConfigError("TLS lock file must have owner-only permissions.")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield directory
    finally:
        os.close(descriptor)


def _ensure_ca(directory):
    authority = directory / "ca"
    if not authority.exists() and not authority.is_symlink():
        with tempfile.TemporaryDirectory(prefix=".ca-", dir=directory) as temporary:
            staging = Path(temporary) / "ca"
            staging.mkdir(mode=0o700)
            key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
            name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Local game development CA")])
            cert = _builder(key, name, name, ca=True, days=3650).sign(key, hashes.SHA256())
            _write_identity(staging, key, cert)
            os.replace(staging, authority)
    private_directory(authority)
    key_path, cert_path = authority / "key.pem", authority / "cert.pem"
    key, cert = _key(key_path), _cert(cert_path)
    if key.public_key() != cert.public_key():
        raise TLSConfigError("Existing development CA certificate and private key do not match.")
    cert.verify_directly_issued_by(cert)
    if not cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca or not (
        cert.extensions.get_extension_for_class(x509.KeyUsage).value.key_cert_sign
    ):
        raise TLSConfigError("Existing development CA cannot sign certificates.")
    return cert_path, key_path


def ensure_server_certificate(state_dir, identity, hosts=None):
    """Return PEM Paths ca_file/cert_file/key_file; never rotate or overwrite existing identities."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", identity):
        raise TLSConfigError("Invalid server certificate identity.")
    if identity == "ca":
        raise TLSConfigError("Reserved server certificate identity.")
    hosts = sorted(set(host_name(host) for host in (hosts or ["localhost", "127.0.0.1"])))
    with _certificate_lock(state_dir) as directory, _certificate_errors():
        ca_cert, ca_key = _ensure_ca(directory)
        authority, signing_key = _cert(ca_cert), _key(ca_key)
        target = directory / identity
        if not target.exists() and not target.is_symlink():
            with tempfile.TemporaryDirectory(prefix=".server-", dir=directory) as temporary:
                staging = Path(temporary) / "server"
                staging.mkdir(mode=0o700)
                key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
                name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hosts[0])])
                cert = (
                    _builder(key, name, authority.subject, ca=False, days=825)
                    .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                    .add_extension(x509.SubjectAlternativeName([_subject(host) for host in hosts]), critical=False)
                    .add_extension(
                        x509.AuthorityKeyIdentifier.from_issuer_public_key(signing_key.public_key()), critical=False
                    )
                    .sign(signing_key, hashes.SHA256())
                )
                _write_identity(staging, key, cert)
                os.replace(staging, target)
        private_directory(target)
        cert_path, key_path = target / "cert.pem", target / "key.pem"
        cert, key = _cert(cert_path), _key(key_path)
        if cert.public_key() != key.public_key():
            raise TLSConfigError("Existing server certificate and private key do not match.")
        policy = PolicyBuilder().store(Store([authority])).max_chain_depth(0)
        for host in hosts:
            policy.build_server_verifier(_subject(host)).verify(cert, [])
        return {"ca_file": ca_cert, "cert_file": cert_path, "key_file": key_path}


def ensure_host_tls(state_dir, listen_host="127.0.0.1", public_host="localhost"):
    public_host = host_name(public_host)
    listen_host = host_name(listen_host)
    if ":" in public_host or ":" in listen_host:
        raise TLSConfigError("The game host currently supports DNS hostnames and IPv4 addresses.")
    material = ensure_server_certificate(state_dir, "host", ["localhost", "127.0.0.1", public_host])
    with _certificate_lock(state_dir), _certificate_errors():
        directory = material["cert_file"].parent
        password, key_store = directory / "password.txt", directory / "host.p12"
        if (key_store.exists() or key_store.is_symlink()) != (password.exists() or password.is_symlink()):
            raise TLSConfigError("Incomplete host key store; restore both host.p12 and password.txt.")
        if not key_store.exists() and not key_store.is_symlink():
            with tempfile.TemporaryDirectory(prefix=".keystore-", dir=directory) as temporary:
                staging = Path(temporary)
                secret = secrets.token_urlsafe(32).encode()
                write_private(staging / "password.txt", secret + b"\n")
                write_private(
                    staging / "host.p12",
                    pkcs12.serialize_key_and_certificates(
                        b"host",
                        _key(material["key_file"]),
                        _cert(material["cert_file"]),
                        [_cert(material["ca_file"])],
                        serialization.BestAvailableEncryption(secret),
                    ),
                )
                os.replace(staging / "password.txt", password)
                os.replace(staging / "host.p12", key_store)
        regular_file(password, private=True)
        regular_file(key_store, private=True)
        stored = pkcs12.load_pkcs12(key_store.read_bytes(), password.read_bytes().strip())
        if (
            stored.key is None
            or stored.cert is None
            or stored.cert.friendly_name != b"host"
            or stored.cert.certificate != _cert(material["cert_file"])
            or stored.key.public_key() != _key(material["key_file"]).public_key()
            or [item.certificate for item in stored.additional_certs] != [_cert(material["ca_file"])]
        ):
            raise TLSConfigError("Existing host key store does not match the host certificate and issuer.")
        config = directory / "config.json"
        write_private_json(
            config,
            {
                "keyStorePath": str(key_store),
                "keyStorePasswordFile": str(password),
                "keyAlias": "host",
                "listenHost": listen_host,
                "publicHost": public_host,
            },
        )
        return config, material["ca_file"]


def validate_host_tls_config(path):
    regular_file(path, private=True)
    config = read_json(path)
    expected = {"keyStorePath", "keyStorePasswordFile", "keyAlias", "listenHost", "publicHost"}
    if (
        not isinstance(config, dict)
        or set(config) != expected
        or not all(isinstance(value, str) and value for value in config.values())
    ):
        raise TLSConfigError("Invalid host TLS configuration fields.")
    for key in ("keyStorePath", "keyStorePasswordFile"):
        if not Path(config[key]).is_absolute():
            raise TLSConfigError("Host TLS key store and password paths must be absolute.")
        regular_file(config[key], private=True)
    host_name(config["publicHost"])
    host_name(config["listenHost"])
    return config


def client_ssl_context(ca_file=None):
    context = ssl.create_default_context()
    # Some Linux builds retain OpenSSL's old cafile path while distributing the
    # actual system bundle elsewhere. Load that bundle alongside the private CA
    # so native HTTPS clients retain ordinary public trust.
    if not context.get_ca_certs():
        for bundle in ("/etc/ssl/certs/ca-certificates.crt", "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem"):
            if Path(bundle).is_file():
                context.load_verify_locations(cafile=bundle)
                break
    if ca_file is not None:
        regular_file(ca_file)
        context.load_verify_locations(cafile=str(ca_file))
    return context


def server_ssl_context(cert_file, key_file):
    regular_file(cert_file)
    regular_file(key_file, private=True)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert_file, key_file)
    return context


def verify_https_connection(url, ca_file=None, timeout=3):
    parsed = urlsplit(https_url(url, ("", "/", "/mcp", "/agent")))
    context = client_ssl_context(ca_file)
    with socket.create_connection((parsed.hostname, parsed.port or 443), timeout=timeout) as stream:
        with context.wrap_socket(stream, server_hostname=parsed.hostname):
            pass


def endpoint_configuration(state_dir, url=None, ca_file=None, port=None):
    config_file = Path(state_dir) / "endpoints.json"
    config = read_json(config_file) if config_file.exists() else {}
    if config and (not isinstance(config, dict) or set(config) != {"hostUrl", "caFile"}):
        raise TLSConfigError("Invalid host endpoint configuration fields.")
    if port is not None and url is not None:
        raise TLSConfigError("Choose either --url or --port.")
    selected_url = url or (host_origin("localhost", port) if port is not None else config.get("hostUrl"))
    if selected_url is None:
        raise TLSConfigError("No HTTPS host endpoint found. Run civ-agent host start or supply --url and --ca-file.")
    selected_url = https_url(selected_url, ("", "/", "/mcp"))
    ca = ca_file if ca_file is not None else config.get("caFile")
    if ca is not None:
        if not isinstance(ca, (str, Path)):
            raise TLSConfigError("Invalid CA certificate path.")
        ca = Path(ca).expanduser().resolve()
        regular_file(ca)
    return selected_url.removesuffix("/mcp"), ca
