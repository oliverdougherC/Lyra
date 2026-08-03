"""Whether a tutor endpoint stays on this machine.

Locality drives the privacy readout and the remote-extraction acknowledgement, so the
answer is deliberately conservative: anything that cannot be proven to be loopback is
treated as remote.
"""

import ipaddress
import socket
from urllib.parse import urlparse


def hostname_of(url: str) -> str | None:
    """The host part of a URL, or None when it has none."""
    return urlparse(url).hostname or None


def is_local_endpoint(url: str) -> bool:
    """True only when every address the endpoint's host resolves to is loopback."""
    hostname = hostname_of(url)
    if hostname is None:
        return False
    if hostname == "localhost":
        return True

    try:
        # socket.gaierror is an OSError, so this covers resolution failure and any
        # other network-stack error. Unknown resolves to remote, the safe direction.
        infos = socket.getaddrinfo(hostname, None)
    except OSError:
        return False

    addresses = {info[4][0] for info in infos}
    if not addresses:
        return False

    try:
        return all(ipaddress.ip_address(address).is_loopback for address in addresses)
    except ValueError:
        # A scoped or otherwise unparseable address. Treat it as remote.
        return False
