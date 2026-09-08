#!/usr/bin/env python3
"""Interactive client for the Talk chat server."""

from __future__ import annotations

import argparse
import socket
import threading


class ChatClient:
    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.socket: socket.socket | None = None
        self.reader = None
        self.running = threading.Event()

    def connect(self) -> None:
        self.socket = socket.create_connection((self.host, self.port), timeout=10)
        self.socket.settimeout(None)
        self.reader = self.socket.makefile("r", encoding="utf-8", newline="\n")
        self.running.set()
        print(f"Connected to {self.host}:{self.port}")

    def send(self, message: str) -> None:
        if not self.socket:
            raise RuntimeError("Client is not connected")
        self.socket.sendall(f"{message.strip()}\n".encode())

    def read_message(self) -> str | None:
        if not self.reader:
            return None
        line = self.reader.readline()
        return line.rstrip("\r\n") if line else None

    def register(self) -> None:
        while self.running.is_set():
            username = input("Screen name: ").strip()
            self.send(f"register {username}")
            response = self.read_message()
            if response is None:
                raise ConnectionError("Server closed the connection")
            print(response)
            if response == f"{username} registered":
                return

    def receive_messages(self) -> None:
        try:
            while self.running.is_set():
                message = self.read_message()
                if message is None:
                    break
                print(message)
                if message == "Client exiting":
                    break
        except (ConnectionError, OSError):
            pass
        finally:
            self.running.clear()

    def run(self) -> None:
        self.connect()
        try:
            self.register()
            receiver = threading.Thread(target=self.receive_messages, daemon=True)
            receiver.start()
            while self.running.is_set():
                try:
                    message = input()
                except EOFError:
                    message = "quit"
                if not message.strip():
                    continue
                self.send(message)
                if message.strip() == "quit":
                    receiver.join(timeout=5)
                    break
        finally:
            self.close()

    def close(self) -> None:
        self.running.clear()
        if self.reader:
            self.reader.close()
            self.reader = None
        if self.socket:
            self.socket.close()
            self.socket = None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host", nargs="?", default="127.0.0.1")
    parser.add_argument("port", nargs="?", type=int, default=8090)
    args = parser.parse_args()

    try:
        ChatClient(args.host, args.port).run()
    except (ConnectionError, OSError) as error:
        print(f"Connection failed: {error}")


if __name__ == "__main__":
    main()
