"""Auto-discover IP Webcam servers across every local IPv4 subnet.

The Android *IP Webcam* app exposes its control surface on a single TCP port
(default 8080) and identifies itself with a ``Server: IP Webcam Server …``
HTTP response header on the root URL.

The scan runs in three phases, ordered from cheapest to most exhaustive:

  1. **Gateway-first.** USB-tethering and Android-hotspot setups make the
     phone the default gateway on its interface. One HEAD request per
     default gateway resolves the happy path in <100 ms.
  2. **TCP-connect prefilter.** For each /24, a fast parallel ``connect``
     to the target port filters out the dead/firewalled hosts that
     dominate cold-scan latency.
  3. **HTTP HEAD on survivors.** Only the hosts that actually opened the
     port get a HEAD request, which is then checked for the IP Webcam
     ``Server`` header. Optional https fallback for hosts the http HEAD
     couldn't confirm.

Typical end-to-end: <100 ms when the phone is the gateway, ~1-2 s on a
cold scan of three /24s.

CLI:

    python -m phone_camera.discover                    # scan every local subnet
    python -m phone_camera.discover --subnet 192.168.1 # explicit /24 prefix
    python -m phone_camera.discover --all              # print every match
    python -m phone_camera.discover --https            # also try https
"""

from __future__ import annotations

import argparse
import re
import socket
import ssl
import subprocess
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from ipaddress import IPv4Address, IPv4Network
from typing import List, Optional, Set, Tuple

_DEFAULT_PORT = 8080
_SERVER_HEADER_SIGNATURE = "IP Webcam"
_IF_IGNORE_PREFIXES: Tuple[str, ...] = ("lo", "docker", "br-", "veth", "virbr")
_IFLINE = re.compile(r"\s*\d+:\s+(\S+)\s+inet\s+(\d+\.\d+\.\d+\.\d+)/\d+")
_ROUTE_DEFAULT = re.compile(r"\s*default\s+via\s+(\d+\.\d+\.\d+\.\d+)\s+dev\s+(\S+)")

# Default timeouts tuned for LAN: real responses arrive in single-digit ms;
# anything past 500 ms is almost certainly a silent drop, not a slow server.
_TCP_CONNECT_TIMEOUT = 0.3
_HTTP_PROBE_TIMEOUT = 0.5
_DEFAULT_WORKERS = 256


def _ip_addr_lines() -> List[str]:
    try:
        out = subprocess.run(
            ["ip", "-4", "-o", "addr", "show"],
            capture_output=True, text=True, check=False, timeout=2.0,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return out.stdout.splitlines() if out.returncode == 0 else []


def _ip_route_lines() -> List[str]:
    try:
        out = subprocess.run(
            ["ip", "-4", "route", "show"],
            capture_output=True, text=True, check=False, timeout=2.0,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return out.stdout.splitlines() if out.returncode == 0 else []


def _scannable_interfaces() -> List[Tuple[str, str]]:
    """Return ``[(ifname, ip), ...]`` for every interface worth scanning.

    Skips loopback, docker/bridge/veth, and 169.254.x.x link-local
    addresses. Caller uses the IP for ``a.b.c`` prefix derivation and
    for skipping the host's own address during probes.
    """
    result: List[Tuple[str, str]] = []
    for line in _ip_addr_lines():
        m = _IFLINE.match(line)
        if not m:
            continue
        ifname, ip = m.group(1), m.group(2)
        if ifname.startswith(_IF_IGNORE_PREFIXES):
            continue
        try:
            addr = IPv4Address(ip)
        except ValueError:
            continue
        if addr.is_loopback or addr.is_link_local:
            continue
        result.append((ifname, ip))
    return result


def _local_subnet_prefixes() -> List[str]:
    """Return every local /24 prefix worth scanning, as ``a.b.c`` strings."""
    seen: List[str] = []
    for _, ip in _scannable_interfaces():
        prefix = ".".join(ip.split(".")[:3])
        if prefix not in seen:
            seen.append(prefix)
    return seen


def _local_subnet_prefix() -> Optional[str]:
    """Back-compat: first scannable /24 prefix as ``a.b.c``, or ``None``."""
    prefixes = _local_subnet_prefixes()
    return prefixes[0] if prefixes else None


def _interface_gateways() -> List[str]:
    """Return default-gateway IPs reachable via scannable interfaces.

    USB tether and Android hotspot make the phone the gateway, so probing
    these first turns a 254-host /24 sweep into one HEAD request in the
    common case. Interfaces filtered by :data:`_IF_IGNORE_PREFIXES` are
    skipped so docker-bridge gateways don't leak in.
    """
    gateways: List[str] = []
    seen: Set[str] = set()
    for line in _ip_route_lines():
        m = _ROUTE_DEFAULT.match(line)
        if not m:
            continue
        gw, ifname = m.group(1), m.group(2)
        if ifname.startswith(_IF_IGNORE_PREFIXES):
            continue
        if gw in seen:
            continue
        seen.add(gw)
        gateways.append(gw)
    return gateways


def _tcp_open(host: str, port: int, timeout: float) -> bool:
    """Fast port-open check: TCP ``connect()`` with a short timeout."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, socket.timeout):
        return False


def _probe(url: str, timeout: float) -> bool:
    """Return True iff the URL's root responds with an IP Webcam ``Server`` header.

    Cheap (HEAD request, no body parse) and specific: the IP Webcam app
    explicitly sets ``Server: IP Webcam Server <ver>`` on every response,
    which is unique enough that random web servers on the same subnet
    won't match.
    """
    ctx = ssl._create_unverified_context() if url.startswith("https") else None
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            if not (200 <= resp.status < 400):
                return False
            server = resp.headers.get("Server", "")
    except Exception:
        return False
    return _SERVER_HEADER_SIGNATURE in server


def _sort_urls(urls: List[str]) -> List[str]:
    """Stable sort by host octet, then scheme (http before https)."""
    def key(u: str):
        scheme, rest = u.split("://", 1)
        host = rest.split(":", 1)[0]
        return (tuple(int(o) for o in host.split(".")), scheme)
    return sorted(set(urls), key=key)


def discover(
    subnet: Optional[str] = None,
    port: int = _DEFAULT_PORT,
    timeout: float = _HTTP_PROBE_TIMEOUT,
    tcp_timeout: float = _TCP_CONNECT_TIMEOUT,
    workers: int = _DEFAULT_WORKERS,
    try_https: bool = False,
    stop_on_first: bool = False,
) -> List[str]:
    """Return every URL responding with the IP Webcam signature on local subnets.

    Args:
        subnet: ``a.b.c`` /24 prefix to scan; ``None`` enumerates every
            local IPv4 interface and scans each. Pass a single prefix when
            you already know which interface the phone is on; leave None
            for the multi-interface default that handles USB-tether +
            WiFi + wired in one call.
        port: TCP port the IP Webcam server is listening on (default 8080).
        timeout: HTTP HEAD timeout (s) for the signature check. 500 ms is
            generous for LAN responses (which arrive in single-digit ms).
        tcp_timeout: TCP ``connect()`` timeout (s) for the port-open
            prefilter. Even shorter than the HTTP timeout — open ports
            answer in <10 ms.
        workers: ThreadPoolExecutor size for the parallel sweep.
        try_https: When True, retry hosts whose port opened but http
            didn't match with an https HEAD. Useful when the app is
            configured to serve only HTTPS.
        stop_on_first: Cut the scan short as soon as one match is found.
            The gateway-first phase always honours this; the TCP-sweep
            phase honours it on a best-effort basis (already-submitted
            connects still run to completion, but no further HEAD
            requests are issued).

    Returns:
        URLs in deterministic host-octet order, http before https for
        the same host.
    """
    if subnet is not None:
        prefixes = [subnet]
        gateways: List[str] = []
    else:
        prefixes = _local_subnet_prefixes()
        gateways = _interface_gateways()
    if not prefixes:
        return []

    self_ips: Set[str] = {ip for _, ip in _scannable_interfaces()}
    found: List[str] = []
    matched_hosts: Set[str] = set()

    # Phase 1: gateway-first short-circuit. Probed in parallel so a
    # slow/dead gateway on one interface doesn't gate the response from
    # a live one on another (e.g. lab router vs. tethered phone). We
    # deliberately don't use ``with ThreadPoolExecutor`` here — its
    # implicit shutdown(wait=True) would still block on the slow probes
    # even after we've found a match. We call shutdown(wait=False)
    # manually so the slow probes finish in the background.
    if gateways:
        gw_pool = ThreadPoolExecutor(max_workers=len(gateways))
        try:
            futures = {gw_pool.submit(_probe, f"http://{gw}:{port}", timeout): gw for gw in gateways}
            for fut in as_completed(futures):
                gw = futures[fut]
                try:
                    if fut.result():
                        found.append(f"http://{gw}:{port}")
                        matched_hosts.add(gw)
                        if stop_on_first:
                            break
                except Exception:
                    pass
        finally:
            gw_pool.shutdown(wait=False)
        if stop_on_first and found:
            return _sort_urls(found)

    # Phase 2: collect /24 candidates (minus host's own IPs, matched gateways).
    candidates: List[str] = []
    seen: Set[str] = set(self_ips) | set(matched_hosts)
    for prefix in prefixes:
        for host in IPv4Network(prefix + ".0/24").hosts():
            h = str(host)
            if h in seen:
                continue
            seen.add(h)
            candidates.append(h)
    if not candidates:
        return _sort_urls(found)

    # Phase 3: TCP-connect prefilter — only hosts with the port actually
    # open survive to the HEAD step. This drops the scan-floor from
    # "1.5 s × dead hosts / workers" to "0.3 s × dead hosts / workers"
    # AND removes the much more expensive HEAD timeout from the dead-host
    # path entirely.
    open_hosts: List[str] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_tcp_open, h, port, tcp_timeout): h for h in candidates}
        for fut in as_completed(futures):
            try:
                if fut.result():
                    open_hosts.append(futures[fut])
            except Exception:
                pass
    if not open_hosts:
        return _sort_urls(found)

    # Phase 4: HTTP HEAD on survivors (http first).
    http_workers = min(workers, max(8, len(open_hosts)))
    with ThreadPoolExecutor(max_workers=http_workers) as pool:
        futures = {pool.submit(_probe, f"http://{h}:{port}", timeout): h for h in open_hosts}
        for fut in as_completed(futures):
            host = futures[fut]
            if stop_on_first and found:
                continue
            try:
                if fut.result():
                    found.append(f"http://{host}:{port}")
                    matched_hosts.add(host)
            except Exception:
                pass

    if not try_https or (stop_on_first and found):
        return _sort_urls(found)

    # Phase 5: https fallback for hosts that opened the port but didn't
    # match via http — useful when the app's "Use HTTPS" toggle is on.
    https_candidates = [h for h in open_hosts if h not in matched_hosts]
    if not https_candidates:
        return _sort_urls(found)
    https_workers = min(workers, max(8, len(https_candidates)))
    with ThreadPoolExecutor(max_workers=https_workers) as pool:
        futures = {
            pool.submit(_probe, f"https://{h}:{port}", timeout): h
            for h in https_candidates
        }
        for fut in as_completed(futures):
            host = futures[fut]
            if stop_on_first and found:
                continue
            try:
                if fut.result():
                    found.append(f"https://{host}:{port}")
                    matched_hosts.add(host)
            except Exception:
                pass

    return _sort_urls(found)


def find_one(**kwargs) -> Optional[str]:
    """Convenience: return the first discovered URL or ``None``.

    Sets ``stop_on_first=True`` so the gateway-first short-circuit
    can return in <100 ms on USB-tether / hotspot setups.
    """
    kwargs.setdefault("stop_on_first", True)
    urls = discover(**kwargs)
    return urls[0] if urls else None


def _main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--subnet", help="/24 prefix as a.b.c (default: scan all interfaces)")
    p.add_argument("--port", type=int, default=_DEFAULT_PORT)
    p.add_argument("--timeout", type=float, default=_HTTP_PROBE_TIMEOUT,
                   help=f"HTTP HEAD timeout in seconds (default {_HTTP_PROBE_TIMEOUT})")
    p.add_argument("--tcp-timeout", type=float, default=_TCP_CONNECT_TIMEOUT,
                   help=f"TCP connect timeout in seconds (default {_TCP_CONNECT_TIMEOUT})")
    p.add_argument("--workers", type=int, default=_DEFAULT_WORKERS)
    p.add_argument("--https", action="store_true", help="also probe https on ports that opened")
    p.add_argument("--all", action="store_true", help="print every match, not just the first")
    p.add_argument("--quiet", "-q", action="store_true", help="suppress 'scanning ...' status")
    args = p.parse_args(argv)

    if args.subnet is not None:
        prefixes = [args.subnet]
    else:
        prefixes = _local_subnet_prefixes()
    if not prefixes:
        print("error: could not auto-detect any local subnet; pass --subnet a.b.c", file=sys.stderr)
        return 2
    if not args.quiet:
        nets = ", ".join(f"{p}.0/24" for p in prefixes)
        gws = _interface_gateways() if args.subnet is None else []
        gw_blurb = f"; gateways first: {', '.join(gws)}" if gws else ""
        print(f"scanning {nets} on port {args.port}{gw_blurb} ...", file=sys.stderr)

    urls = discover(
        subnet=args.subnet,
        port=args.port,
        timeout=args.timeout,
        tcp_timeout=args.tcp_timeout,
        workers=args.workers,
        try_https=args.https,
        stop_on_first=not args.all,
    )
    if not urls:
        if not args.quiet:
            print("no IP Webcam server found", file=sys.stderr)
        return 1
    for url in (urls if args.all else urls[:1]):
        print(url)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
