#!/usr/bin/env python3
"""Threaded TCP chat server using a newline-delimited text protocol."""

from __future__ import annotations

import argparse
import socketserver
import threading


class ChatState:
    def __init__(self) -> None:
        self.clients: dict[ChatHandler, str | None] = {}
        self.lock = threading.RLock()

    def connect(self, client: ChatHandler) -> int:
        with self.lock:
            self.clients[client] = None
            return len(self.clients)

    def disconnect(self, client: ChatHandler) -> int:
        with self.lock:
            self.clients.pop(client, None)
            return len(self.clients)

    def register(self, client: ChatHandler, username: str) -> str:
        if not username:
            return "username required"
        if any(character.isspace() for character in username):
            return "username must not contain spaces"
        with self.lock:
            if self.clients.get(client):
                return "already registered"
            if username in self.clients.values():
                return "username taken"
            self.clients[client] = username
        print(f"{username} registered", flush=True)
        return f"{username} registered"

    def username(self, client: ChatHandler) -> str | None:
        with self.lock:
            return self.clients.get(client)

    def recipients(self) -> list[ChatHandler]:
        with self.lock:
            return [client for client, name in self.clients.items() if name]

    def find(self, username: str) -> ChatHandler | None:
        with self.lock:
            return next(
                (client for client, name in self.clients.items() if name == username),
                None,
            )

    def usernames(self) -> list[str]:
        with self.lock:
            return sorted(name for name in self.clients.values() if name)


class ChatHandler(socketserver.StreamRequestHandler):
    server: ChatServer

    def setup(self) -> None:
        super().setup()
        self.send_lock = threading.Lock()
        count = self.server.state.connect(self)
        print(f"new client connected\n{count} active clients", flush=True)

    def finish(self) -> None:
        count = self.server.state.disconnect(self)
        print(f"a client disconnected\n{count} active clients", flush=True)
        super().finish()

    def send_message(self, message: str) -> None:
        try:
            with self.send_lock:
                self.wfile.write(f"{message}\n".encode())
                self.wfile.flush()
        except (ConnectionError, OSError):
            pass

    def handle(self) -> None:
        for raw_message in self.rfile:
            message = raw_message.decode("utf-8", errors="replace").strip()
            if not message:
                continue
            if not self.process(message):
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
            self.send_message(self.server.state.register(self, parameters))
            return True

        if command == "send_all":
            if not parameters:
                self.send_message("message required")
                return True
            outgoing = f"message from {sender}: {parameters}"
            for client in self.server.state.recipients():
                client.send_message(outgoing)
            print(f"{outgoing} (to everyone)", flush=True)

        elif command == "send_to":
            target_name, separator, text = parameters.partition(" ")
            if not separator or not text.strip():
                self.send_message("Usage: send_to <username> <message>")
                return True
            target = self.server.state.find(target_name)
            if not target:
                self.send_message(f"User {target_name} not found")
                return True
            outgoing = f"message from {sender}: {text.strip()}"
            target.send_message(outgoing)
            print(f"{outgoing} (to {target_name})", flush=True)

        elif command == "user_list":
            users = ", ".join(self.server.state.usernames())
            self.send_message(f"Online users: {users}")

        elif command == "quit":
            self.send_message("Client exiting")
            return False

        return True


class ChatServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: tuple[str, int]) -> None:
        self.state = ChatState()
        super().__init__(address, ChatHandler)


def create_server(host: str, port: int) -> ChatServer:
    return ChatServer((host, port))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host", nargs="?", default="127.0.0.1")
    parser.add_argument("port", nargs="?", type=int, default=8090)
    args = parser.parse_args()

    with create_server(args.host, args.port) as server:
        host, port = server.server_address
        print(f"Chat server listening on {host}:{port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nStopping chat server.", flush=True)


if __name__ == "__main__":
    main()
