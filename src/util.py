import ipaddress
import re

_IPV4 = re.compile(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$")
_HOSTNAME = re.compile(
    r"^(?=.{1,253}$)([a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[A-Za-z]{2,63}$"
)


def parse_endpoint(token):
    """Terima 'host:port'. Balikkan (host, port) atau None.

    Data proxy publik tidak dipercaya: suatu baris yang lolos regex
    belum tentu host yang sah, jadi IP divalidasi dengan ipaddress
    dan nama host dengan pola DNS standar.
    """
    if not isinstance(token, str):
        return None
    token = token.strip()
    if token.count(":") != 1:
        return None
    host, _, port_s = token.partition(":")
    if not port_s.isdigit():
        return None
    port = int(port_s)
    if not 1 <= port <= 65535:
        return None
    if not host:
        return None
    if _IPV4.match(host):
        try:
            ipaddress.IPv4Address(host)
        except ValueError:
            return None
        return host, port
    if _HOSTNAME.match(host):
        return host.lower(), port
    return None


def now_iso():
    import datetime

    return (
        datetime.datetime.now(datetime.timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def human(n):
    return f"{n:,}".replace(",", ".")
