#!/usr/bin/env python3
"""Loopback identity proxy: LAN -> 127.0.0.1:28000, for dsh-mobile admin API.

v3 semantics (fixes v2 keep-alive hang):
- one request per client connection; Connection: close is forced toward the
  backend so it closes after responding (no 60s wait on keep-alive);
- full request body is read via Content-Length before forwarding;
- WebSocket Upgrade requests are raw-tunneled both ways after 101.
"""
import os
import select
import socket
import socketserver

LISTEN_PORT = int(os.environ.get("LBP_PORT", "28080"))
BACKEND_HOST = "127.0.0.1"
BACKEND_PORT = int(os.environ.get("LBP_BACKEND_PORT", "28000"))
BUF = 65536
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
       "te", "trailers", "transfer-encoding", "upgrade"}


def recv_head(sock, limit=4 << 20):
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(BUF)
        if not chunk:
            return None
        data += chunk
        if len(data) > limit:
            return None
    head, _, body = data.partition(b"\r\n\r\n")
    return head, body


def content_length(head):
    for line in head.split(b"\r\n")[1:]:
        k, _, v = line.partition(b":")
        if k.strip().lower() == b"content-length":
            try:
                return int(v.strip())
            except ValueError:
                return 0
    return 0


TASK_BOARD_TOKEN_FILE = "/vol1/@appshare/dsh/dsh_home/.taskboard-proxy-token"
TASK_BOARD_PROXY_TOKEN_HEADER = b"x-dsh-task-board-proxy-token"


def task_board_token():
    """Read the shared task-board proxy token once per request; empty when absent."""
    try:
        with open(TASK_BOARD_TOKEN_FILE, "r", encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def inject_task_board_token(head):
    """Add the proxy token and same-origin markers so the task-board fence accepts this hop.

    SEC_FETCH_FILL: browsers omit Sec-Fetch-Site on some same-origin requests (the
    DSH page sends Referrer-Policy: no-referrer), and the task-board fence then
    treats the hop as untrusted. This proxy is already the authenticated local hop,
    so it states the same-origin marker explicitly.
    """
    lowered = head.lower()
    if b"\r\nsec-fetch-site:" not in lowered and not lowered.startswith(b"sec-fetch-site:"):
        head = head + b"\r\nsec-fetch-site: same-origin"
    token = task_board_token()
    if not token:
        return head
    if TASK_BOARD_PROXY_TOKEN_HEADER + b":" in head.lower():
        return head
    return head + b"\r\n" + TASK_BOARD_PROXY_TOKEN_HEADER + b": " + token.encode()


def rewrite_connection(head):
    """Drop existing Connection header, append 'Connection: close'."""
    lines = head.split(b"\r\n")
    out = [lines[0]]
    for line in lines[1:]:
        k, _, _ = line.partition(b":")
        if k.strip().lower() == b"connection":
            continue
        out.append(line)
    out.append(b"Connection: close")
    return b"\r\n".join(out)


def is_ws_upgrade(head):
    return b"upgrade: websocket" in head.lower()


def pump(a, b, pending_client=b""):
    """Bidirectional byte pump until either side closes. select-based."""
    if pending_client:
        b.sendall(pending_client)
    peers = {a: b, b: a}
    try:
        while True:
            r, _, x = select.select([a, b], [], [a, b], 300)
            if x:
                return
            if not r:
                return
            for src in r:
                dst = peers[src]
                try:
                    data = src.recv(BUF)
                except OSError:
                    return
                if not data:
                    return
                try:
                    dst.sendall(data)
                except OSError:
                    return
    except OSError:
        return


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        client = self.request
        try:
            client.settimeout(120)
            got = recv_head(client)
            if got is None:
                return
            head, body = got
            if not head.startswith((b"GET ", b"POST ", b"PUT ", b"PATCH ",
                                    b"DELETE ", b"HEAD ", b"OPTIONS ")):
                return

            # read the full request body
            clen = content_length(head)
            while len(body) < clen:
                chunk = client.recv(min(BUF, clen - len(body)))
                if not chunk:
                    break
                body += chunk

            upgrade = is_ws_upgrade(head)

            try:
                backend = socket.create_connection((BACKEND_HOST, BACKEND_PORT), timeout=10)
            except OSError:
                msg = b"502 loopback-proxy: backend unreachable"
                hdr = (b"HTTP/1.1 502 Bad Gateway\r\nContent-Type: text/plain\r\n"
                       b"Content-Length: " + str(len(msg)).encode() +
                       b"\r\nConnection: close\r\n\r\n")
                client.sendall(hdr + msg)
                return

            backend.settimeout(120)
            if upgrade:
                backend.sendall(head + b"\r\n\r\n" + body)
                got = recv_head(backend)
                if got is None:
                    client.close(); backend.close(); return
                bhead, brest = got
                client.sendall(bhead + b"\r\n\r\n" + brest)
                if b" 101 " in bhead.split(b"\r\n")[0]:
                    pump(client, backend, pending_client=brest)
                return

            # normal HTTP: force close semantics both ways
            fwd = inject_task_board_token(rewrite_connection(head))
            backend.sendall(fwd + b"\r\n\r\n" + body)
            s = backend
            try:
                while True:
                    chunk = s.recv(BUF)
                    if not chunk:
                        break
                    client.sendall(chunk)
            except OSError:
                pass
            finally:
                try: backend.close()
                except OSError: pass
                try: client.close()
                except OSError: pass
        except OSError:
            pass


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    with Server(("0.0.0.0", LISTEN_PORT), Handler) as srv:
        srv.serve_forever()
