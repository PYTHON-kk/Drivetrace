"""In-process network egress guard for the assistant (defence in depth, NFR-05/07).

After `install()`, any socket connection to a non-loopback address raises PermissionError. This does not
replace OS isolation (a container with no external network, solution_journey Defect 10); it makes an
accidental or injected outbound call fail loudly even when the OS layer is misconfigured.
"""

import ipaddress
import socket

_installed = False
_original_connect = socket.socket.connect
_original_create_connection = socket.create_connection
LOOPBACK_NAMES = {"localhost", "localhost.localdomain"}


def _is_loopback(host) -> bool:
    if isinstance(host, bytes):
        host = host.decode()
    if host in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False  # a hostname that is not loopback would need DNS: refuse


def _guarded_connect(self, address):
    if self.family in (socket.AF_INET, socket.AF_INET6) and not _is_loopback(address[0]):
        raise PermissionError(f"DriveTrace egress guard: outbound connection to {address[0]!r} blocked")
    return _original_connect(self, address)


def _guarded_create_connection(address, *args, **kwargs):
    if not _is_loopback(address[0]):
        raise PermissionError(f"DriveTrace egress guard: outbound connection to {address[0]!r} blocked")
    return _original_create_connection(address, *args, **kwargs)


def install():
    global _installed
    if not _installed:
        socket.socket.connect = _guarded_connect
        socket.create_connection = _guarded_create_connection
        _installed = True


def uninstall():  # tests only
    global _installed
    socket.socket.connect = _original_connect
    socket.create_connection = _original_create_connection
    _installed = False
