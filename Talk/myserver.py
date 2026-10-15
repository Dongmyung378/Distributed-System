#!/usr/bin/env python3
"""Bounded threaded TCP chat server using the original newline text protocol."""

from __future__ import annotations

import argparse
import math
import queue
import select
import socket
import socketserver
import threading
import time
import unicodedata
from collections import Counter


class ChatState:
    """Protect membership and publish names only after confirmation is queued."""

    def __init__(self) -> None:
        self.clients: dict[ChatHandler, str | None] = {}
        self.names: dict[str, ChatHandler] = {}
        self.lock = threading.RLock()

    def connect(self, client: ChatHandler) -> None:
        with self.lock:
            self.clients[client] = None

    def disconnect(self, client: ChatHandler) -> None:
        with self.lock:
            username = self.clients.pop(client, None)
            if username and self.names.get(username) is client:
                del self.names[username]

    def register(self, client: ChatHandler, username: str) -> str:
        if not username:
            response = "username required"
        elif any(c.isspace() for c in username):
            response = "username must not contain spaces"
        elif any(unicodedata.category(c) == "Cc" for c in username):
            response = "username contains control characters"
        elif len(username.encode("utf-8")) > client.server.max_name_bytes:
            response = "username too long"
        else:
            with self.lock:
                if client not in self.clients or client.closing.is_set():
                    return "connection closed"
                if self.clients[client]:
                    response = "already registered"
                elif username in self.names:
                    response = "username taken"
                else:
                    response = f"{username} registered"
                    # No queue lock is held while acquiring the registry lock.
                    # A broadcaster cannot discover this peer before its ACK.
                    if client.send_message(response):
                        self.clients[client] = username
                        self.names[username] = client
                        client.registered.set()
                    return response
        client.send_message(response)
        return response

    def username(self, client: ChatHandler) -> str | None:
        with self.lock:
            return self.clients.get(client)

    def recipients(self) -> list[ChatHandler]:
        with self.lock:
            return list(self.names.values())

    def connections(self) -> list[ChatHandler]:
        with self.lock:
            return list(self.clients)

    def find(self, username: str) -> ChatHandler | None:
        with self.lock:
            return self.names.get(username)

    def usernames(self) -> list[str]:
        with self.lock:
            return sorted(self.names)


class ChatHandler(socketserver.BaseRequestHandler):
    server: ChatServer

    def setup(self) -> None:
        self.closing = threading.Event()
        self.registered = threading.Event()
        self.outgoing: queue.Queue[bytes] = queue.Queue(self.server.outbound_queue_size)
        self._enqueue_lock = threading.Lock()
        self._writer_stop = threading.Event()
        self._close_lock = threading.Lock()
        self._socket_closed = False
        self.connected_at = time.monotonic()
        self.request.setblocking(False)
        if self.server.send_buffer_bytes is not None:
            self.request.setsockopt(
                socket.SOL_SOCKET, socket.SO_SNDBUF, self.server.send_buffer_bytes
            )
        self.server.state.connect(self)
        self.server.count("connections_accepted")
        self.writer = threading.Thread(
            target=self._write_messages, name="talk-sender", daemon=True
        )
        self.writer.start()

    def send_message(self, message: str) -> bool:
        """Enqueue a frame; success does not imply delivery or acknowledgement."""
        with self._enqueue_lock:
            if self.closing.is_set() or self._writer_stop.is_set():
                return False
            try:
                self.outgoing.put_nowait(f"{message}\n".encode())
                self.server.count("messages_enqueued")
                return True
            except queue.Full:
                pass
        self.server.count("queue_overflows")
        self.close()
        return False

    def _write_messages(self) -> None:
        try:
            while not self._writer_stop.is_set() or not self.outgoing.empty():
                try:
                    frame = self.outgoing.get(timeout=0.05)
                except queue.Empty:
                    continue
                try:
                    remaining = memoryview(frame)
                    deadline = time.monotonic() + self.server.send_timeout
                    while remaining:
                        timeout = deadline - time.monotonic()
                        if timeout <= 0:
                            raise TimeoutError("chat send deadline exceeded")
                        _, writable, _ = select.select(
                            [], [self.request], [], min(timeout, 0.1)
                        )
                        if self._socket_closed:
                            return
                        if not writable:
                            continue
                        try:
                            sent = self.request.send(remaining)
                        except BlockingIOError:
                            continue
                        if sent == 0:
                            raise ConnectionError("closed chat connection")
                        remaining = remaining[sent:]
                    self.server.count("messages_sent")
                    self.server.count("bytes_sent", len(frame))
                finally:
                    self.outgoing.task_done()
        except (OSError, ValueError):
            if not self._socket_closed:
                self.server.count("send_failures")
            self.close()

    def close(self) -> None:
        """Abort this peer and release its name without waiting for its reader."""
        with self._close_lock:
            if self._socket_closed:
                return
            self.closing.set()
            self._writer_stop.set()
            self._socket_closed = True
            try:
                self.request.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self.server.state.disconnect(self)

    def finish(self) -> None:
        with self._enqueue_lock:
            self.closing.set()
            self._writer_stop.set()
        # Normal quit/rejection drains queued responses for one bounded window.
        self.writer.join(timeout=self.server.send_timeout + 0.1)
        self.close()
        self.writer.join(timeout=0.2)
        self.server.count("connections_closed")

    def handle(self) -> None:
        buffered = bytearray()
        try:
            while not self.closing.is_set() and not self.server.stopping.is_set():
                wait = 0.1
                if not self.registered.is_set():
                    remaining = (
                        self.connected_at
                        + self.server.registration_timeout
                        - time.monotonic()
                    )
                    if remaining <= 0:
                        self.server.count("registration_timeouts")
                        self.send_message("registration timeout")
                        return
                    wait = min(remaining, wait)
                readable, _, _ = select.select([self.request], [], [], wait)
                if not readable:
                    continue
                try:
                    chunk = self.request.recv(min(4096, self.server.max_line_bytes + 1))
                except BlockingIOError:
                    continue
                if not chunk:
                    return
                buffered.extend(chunk)
                while b"\n" in buffered:
                    raw_message, _, remainder = buffered.partition(b"\n")
                    buffered = bytearray(remainder)
                    if len(raw_message) > self.server.max_line_bytes:
                        self.server.count("oversized_frames")
                        self.send_message("message too long")
                        return
                    try:
                        message = raw_message.decode("utf-8").strip()
                    except UnicodeDecodeError:
                        self.server.count("invalid_utf8_frames")
                        self.send_message("invalid utf-8")
                        return
                    self.server.count("frames_received")
                    if message and not self.process(message):
                        return
                if len(buffered) > self.server.max_line_bytes:
                    self.server.count("oversized_frames")
                    self.send_message("message too long")
                    return
        except (ConnectionError, OSError, ValueError):
            return

    def process(self, message: str) -> bool:
        command, _, parameters = message.partition(" ")
        parameters = parameters.strip()
        valid_commands = {"register", "send_all", "send_to", "user_list", "quit"}
        sender = self.server.state.username(self)
        if command != "register" and not sender:
            self.send_message("not registered")
            return True
        if command not in valid_commands:
            self.send_message("unknown command")
            return True
        if command == "register":
            self.server.state.register(self, parameters)
        elif command == "send_all":
            if not parameters:
                self.send_message("message required")
                return True
            outgoing = f"message from {sender}: {parameters}"
            for client in self.server.state.recipients():
                client.send_message(outgoing)
        elif command == "send_to":
            target_name, separator, text = parameters.partition(" ")
            if not separator or not text.strip():
                self.send_message("Usage: send_to <username> <message>")
                return True
            target = self.server.state.find(target_name)
            if not target:
                self.send_message(f"User {target_name} not found")
                return True
            target.send_message(f"message from {sender}: {text.strip()}")
        elif command == "user_list":
            self.send_message(
                f"Online users: {', '.join(self.server.state.usernames())}"
            )
        elif command == "quit":
            self.send_message("Client exiting")
            return False
        return True


class ChatServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        *,
        max_clients: int = 128,
        max_line_bytes: int = 8192,
        max_name_bytes: int = 64,
        registration_timeout: float = 30.0,
        outbound_queue_size: int = 128,
        send_timeout: float = 2.0,
        shutdown_timeout: float = 3.0,
        send_buffer_bytes: int | None = None,
    ) -> None:
        options = {
            "max_clients": max_clients,
            "max_line_bytes": max_line_bytes,
            "max_name_bytes": max_name_bytes,
            "outbound_queue_size": outbound_queue_size,
        }
        if send_buffer_bytes is not None:
            options["send_buffer_bytes"] = send_buffer_bytes
        for name, value in options.items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
            setattr(self, name, value)
        self.send_buffer_bytes = send_buffer_bytes
        for name, value in {
            "registration_timeout": registration_timeout,
            "send_timeout": send_timeout,
            "shutdown_timeout": shutdown_timeout,
        }.items():
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a positive finite number")
            setattr(self, name, float(value))
        self.state = ChatState()
        self.stopping = threading.Event()
        self._capacity = threading.BoundedSemaphore(max_clients)
        self._counters: Counter[str] = Counter()
        self._counter_lock = threading.Lock()
        self._workers: set[threading.Thread] = set()
        self._worker_condition = threading.Condition()
        self._pending_requests = 0
        super().__init__(address, ChatHandler)

    def count(self, name: str, amount: int = 1) -> None:
        with self._counter_lock:
            self._counters[name] += amount

    def diagnostics(self) -> dict[str, int]:
        keys = (
            "connections_accepted",
            "connections_rejected",
            "connections_closed",
            "messages_enqueued",
            "messages_sent",
            "bytes_sent",
            "frames_received",
            "send_failures",
            "queue_overflows",
            "registration_timeouts",
            "oversized_frames",
            "invalid_utf8_frames",
        )
        with self._counter_lock:
            result = {name: self._counters[name] for name in keys}
        with self.state.lock:
            result["active_connections"] = len(self.state.clients)
            result["registered_users"] = len(self.state.names)
        with self._worker_condition:
            result["handler_threads"] = len(self._workers)
            result["pending_requests"] = self._pending_requests
        return result

    snapshot = diagnostics

    def verify_request(self, request: socket.socket, client_address: tuple) -> bool:
        if self.stopping.is_set() or not self._capacity.acquire(blocking=False):
            self.count("connections_rejected")
            try:
                request.settimeout(0.05)
                request.sendall(b"server busy\n")
            except OSError:
                pass
            return False
        return True

    def process_request(self, request: socket.socket, client_address: tuple) -> None:
        with self._worker_condition:
            self._pending_requests += 1
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._capacity.release()
            with self._worker_condition:
                self._pending_requests -= 1
                self._worker_condition.notify_all()
            raise

    def process_request_thread(
        self, request: socket.socket, client_address: tuple
    ) -> None:
        worker = threading.current_thread()
        with self._worker_condition:
            self._workers.add(worker)
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._capacity.release()
            with self._worker_condition:
                self._workers.discard(worker)
                self._pending_requests -= 1
                self._worker_condition.notify_all()

    def server_close(self) -> None:
        self.stopping.set()
        for client in self.state.connections():
            client.close()
        super().server_close()
        deadline = time.monotonic() + self.shutdown_timeout
        with self._worker_condition:
            while self._pending_requests:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._worker_condition.wait(timeout=remaining)


def create_server(host: str, port: int, **options) -> ChatServer:
    return ChatServer((host, port), **options)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host", nargs="?", default="127.0.0.1")
    parser.add_argument("port", nargs="?", type=int, default=8090)
    parser.add_argument("--max-clients", type=int, default=128)
    parser.add_argument("--max-line-bytes", type=int, default=8192)
    parser.add_argument("--max-name-bytes", type=int, default=64)
    parser.add_argument("--registration-timeout", type=float, default=30.0)
    parser.add_argument("--outbound-queue-size", type=int, default=128)
    parser.add_argument("--send-timeout", type=float, default=2.0)
    args = parser.parse_args()
    with create_server(
        args.host,
        args.port,
        max_clients=args.max_clients,
        max_line_bytes=args.max_line_bytes,
        max_name_bytes=args.max_name_bytes,
        registration_timeout=args.registration_timeout,
        outbound_queue_size=args.outbound_queue_size,
        send_timeout=args.send_timeout,
    ) as server:
        host, port = server.server_address
        print(f"Chat server listening on {host}:{port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nStopping chat server.", flush=True)


if __name__ == "__main__":
    main()
