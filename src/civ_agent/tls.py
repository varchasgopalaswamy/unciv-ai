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
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


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


def _openssl(*arguments):
    try:
        return subprocess.run(["openssl", *map(str, arguments)], check=True, capture_output=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        # Tool errors can contain PEM contents or password diagnostics; report only the operation.
        raise TLSConfigError(
            f"OpenSSL {arguments[0]} failed. Check certificate files and that openssl is installed."
        ) from None


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
            _openssl(
                "req",
                "-x509",
                "-newkey",
                "rsa:3072",
                "-noenc",
                "-sha256",
                "-days",
                "3650",
                "-subj",
                "/CN=Local game development CA",
                "-keyout",
                staging / "key.pem",
                "-out",
                staging / "cert.pem",
                "-addext",
                "basicConstraints=critical,CA:TRUE",
                "-addext",
                "keyUsage=critical,keyCertSign,cRLSign",
            )
            for file in staging.iterdir():
                file.chmod(0o600)
            os.replace(staging, authority)
    private_directory(authority)
    key, cert = authority / "key.pem", authority / "cert.pem"
    regular_file(key, private=True)
    regular_file(cert, private=True)
    _openssl("x509", "-in", cert, "-noout", "-checkend", "86400")
    if _openssl("pkey", "-in", key, "-pubout") != _openssl("x509", "-in", cert, "-pubkey", "-noout"):
        raise TLSConfigError("Existing development CA certificate and private key do not match.")
    _openssl("verify", "-CAfile", cert, cert)
    return cert, key


def ensure_server_certificate(state_dir, identity, hosts=None):
    """Return PEM Paths ca_file/cert_file/key_file; never rotate or overwrite existing identities."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", identity):
        raise TLSConfigError("Invalid server certificate identity.")
    hosts = sorted(set(host_name(host) for host in (hosts or ["localhost", "127.0.0.1"])))
    with _certificate_lock(state_dir) as directory:
        ca_cert, ca_key = _ensure_ca(directory)
        target = directory / identity
        if identity == "ca":
            raise TLSConfigError("Reserved server certificate identity.")
        if not target.exists() and not target.is_symlink():
            with tempfile.TemporaryDirectory(prefix=".server-", dir=directory) as temporary:
                staging = Path(temporary) / "server"
                staging.mkdir(mode=0o700)
                sans = []
                for host in hosts:
                    try:
                        ipaddress.ip_address(host)
                        sans.append("IP:" + host)
                    except ValueError:
                        sans.append("DNS:" + host)
                extension = Path(temporary) / "extensions.cnf"
                extension.write_text(
                    "basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n"
                    "extendedKeyUsage=serverAuth\nsubjectAltName=" + ",".join(sans) + "\n"
                )
                request = Path(temporary) / "server.csr"
                _openssl(
                    "req",
                    "-new",
                    "-newkey",
                    "rsa:3072",
                    "-noenc",
                    "-sha256",
                    "-subj",
                    "/CN=" + hosts[0],
                    "-keyout",
                    staging / "key.pem",
                    "-out",
                    request,
                )
                _openssl(
                    "x509",
                    "-req",
                    "-in",
                    request,
                    "-CA",
                    ca_cert,
                    "-CAkey",
                    ca_key,
                    "-set_serial",
                    "0x" + secrets.token_hex(20),
                    "-days",
                    "825",
                    "-sha256",
                    "-extfile",
                    extension,
                    "-out",
                    staging / "cert.pem",
                )
                for file in staging.iterdir():
                    file.chmod(0o600)
                os.replace(staging, target)
        private_directory(target)
        cert, key = target / "cert.pem", target / "key.pem"
        regular_file(cert, private=True)
        regular_file(key, private=True)
        _openssl("x509", "-in", cert, "-noout", "-checkend", "86400")
        _openssl("verify", "-purpose", "sslserver", "-CAfile", ca_cert, cert)
        for host in hosts:
            try:
                ipaddress.ip_address(host)
                check = "-verify_ip"
            except ValueError:
                check = "-verify_hostname"
            _openssl("verify", "-CAfile", ca_cert, check, host, cert)
        try:
            ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(cert, key)
        except ssl.SSLError:
            raise TLSConfigError("Existing server certificate and private key do not match.") from None
        return {"ca_file": ca_cert, "cert_file": cert, "key_file": key}


def ensure_host_tls(state_dir, listen_host="127.0.0.1", public_host="localhost"):
    public_host = host_name(public_host)
    listen_host = host_name(listen_host)
    if ":" in public_host or ":" in listen_host:
        raise TLSConfigError("The game host currently supports DNS hostnames and IPv4 addresses.")
    material = ensure_server_certificate(state_dir, "host", ["localhost", "127.0.0.1", public_host])
    with _certificate_lock(state_dir):
        directory = material["cert_file"].parent
        password, key_store = directory / "password.txt", directory / "host.p12"
        if key_store.exists() != password.exists():
            raise TLSConfigError("Incomplete host key store; restore both host.p12 and password.txt.")
        if not key_store.exists():
            # The password is read by OpenSSL from a file, never from process arguments.
            with tempfile.TemporaryDirectory(prefix=".keystore-", dir=directory) as temporary:
                staging = Path(temporary)
                write_private(staging / "password.txt", (secrets.token_urlsafe(32) + "\n").encode())
                _openssl(
                    "pkcs12",
                    "-export",
                    "-name",
                    "host",
                    "-inkey",
                    material["key_file"],
                    "-in",
                    material["cert_file"],
                    "-certfile",
                    material["ca_file"],
                    "-out",
                    staging / "host.p12",
                    "-passout",
                    "file:" + str(staging / "password.txt"),
                )
                (staging / "host.p12").chmod(0o600)
                os.replace(staging / "password.txt", password)
                os.replace(staging / "host.p12", key_store)
        regular_file(password, private=True)
        regular_file(key_store, private=True)
        _openssl("pkcs12", "-in", key_store, "-passin", "file:" + str(password), "-noout")
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
    # actual system bundle elsewhere. Loading it also makes lazy capath roots
    # available when exporting a combined PEM bundle for Codex.
    if not context.get_ca_certs():
        for bundle in ("/etc/ssl/certs/ca-certificates.crt", "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem"):
            if Path(bundle).is_file():
                context.load_verify_locations(cafile=bundle)
                break
    if ca_file is not None:
        regular_file(ca_file)
        context.load_verify_locations(cafile=str(ca_file))
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


def codex_trust_environment(ca_file, environment, workspace):
    """Keep public/existing custom trust while adding the game's private CA for Codex HTTPS."""
    result = environment.copy()
    if ca_file is None:
        return result
    context = client_ssl_context(ca_file)
    existing = environment.get("CODEX_CA_CERTIFICATE") or environment.get("SSL_CERT_FILE")
    if existing:
        context.load_verify_locations(cafile=str(regular_file(existing)))
    certificates = sorted(ssl.DER_cert_to_PEM_cert(cert) for cert in context.get_ca_certs(binary_form=True))
    bundle = Path(workspace) / ".tls-ca-bundle.pem"
    write_private(bundle, "".join(certificates).encode())
    result["CODEX_CA_CERTIFICATE"] = str(bundle)
    result["SSL_CERT_FILE"] = str(bundle)
    return result
