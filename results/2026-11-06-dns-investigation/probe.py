"""Одинаковые DNS-пробы на Mac и в отдельном контейнере, без API-ключа."""

import argparse
import concurrent.futures
import http.client
import json
import os
import socket
import ssl
import struct
import time

DOMAIN = "inference.airi.net"


def query(server, protocol, timeout=2):
    started = time.monotonic()
    result = {"server": server, "protocol": protocol}
    identifier = os.urandom(2)
    question = b"".join(bytes([len(part)]) + part.encode() for part in DOMAIN.split("."))
    packet = identifier + struct.pack("!HHHHH", 0x100, 1, 0, 0, 0) + question + b"\0\0\1\0\1"
    try:
        kind = socket.SOCK_DGRAM if protocol == "UDP" else socket.SOCK_STREAM
        with socket.socket(socket.AF_INET, kind) as connection:
            connection.settimeout(timeout)
            connection.connect((server, 53))
            if protocol == "UDP":
                connection.send(packet)
                response = connection.recv(4096)
            else:
                connection.sendall(struct.pack("!H", len(packet)) + packet)

                def receive(length):
                    data = bytearray()
                    while len(data) < length:
                        chunk = connection.recv(length - len(data))
                        if not chunk:
                            raise ConnectionError("DNS stream closed")
                        data.extend(chunk)
                    return data

                response = receive(struct.unpack("!H", receive(2))[0])
            if response[:2] != identifier or len(response) < 12:
                raise ValueError("Unexpected DNS reply")
            _, flags, _, answers, _, _ = struct.unpack("!HHHHHH", response[:12])
            result.update(rcode=flags & 15, answers=answers)
    except (OSError, ValueError, ConnectionError) as error:
        result.update(error=type(error).__name__, errno=getattr(error, "errno", None))
    result["seconds"] = round(time.monotonic() - started, 3)
    return result


def doh():
    connection = http.client.HTTPSConnection("1.1.1.1", timeout=5)
    try:
        connection.request(
            "GET",
            f"/dns-query?name={DOMAIN}&type=A",
            headers={"Accept": "application/dns-json", "Host": "cloudflare-dns.com"},
        )
        response = connection.getresponse()
        body = json.loads(response.read(65536))
        return {
            "status": response.status,
            "rcode": body.get("Status"),
            "addresses": [item["data"] for item in body.get("Answer", []) if item["type"] == 1],
        }
    except (OSError, ValueError, http.client.HTTPException) as error:
        return {"error": type(error).__name__}
    finally:
        connection.close()


def https(address):
    try:
        with socket.create_connection((address, 443), timeout=5) as connection:
            with ssl.create_default_context().wrap_socket(
                connection, server_hostname=DOMAIN
            ) as tls:
                tls.sendall(
                    f"GET / HTTP/1.1\r\nHost: {DOMAIN}\r\nConnection: close\r\n\r\n".encode()
                )
                return {
                    "status_line": tls.recv(4096).split(b"\r\n", 1)[0].decode(),
                    "tls": tls.version(),
                }
    except OSError as error:
        return {"error": type(error).__name__}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--container", action="store_true")
    parser.add_argument("--provider-ip", default="194.190.0.250")
    parser.add_argument("--timeout", type=float, default=2)
    args = parser.parse_args()
    servers = ["1.1.1.1", "8.8.8.8", "192.168.0.1"]
    if args.container:
        servers += ["127.0.0.11", "192.168.65.7", "192.168.65.1"]
    report = {"scope": "container" if args.container else "host", "domain": DOMAIN}
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        futures = [
            pool.submit(query, server, protocol, args.timeout)
            for server in servers
            for protocol in ("UDP", "TCP")
        ]
        report["queries"] = [future.result() for future in futures]
    try:
        report["native"] = sorted({entry[4][0] for entry in socket.getaddrinfo(DOMAIN, 443)})
    except OSError as error:
        report["native"] = {"error": type(error).__name__, "errno": error.errno}
    report["doh"] = doh()
    report["provider_https_by_ip"] = https(args.provider_ip)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
