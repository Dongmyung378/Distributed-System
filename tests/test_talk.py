from __future__ import annotations

import socket
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "Talk"))

from myserver import create_server


class LineClient:
    def __init__(self, port: int) -> None:
        self.socket = socket.create_connection(("127.0.0.1", port), timeout=2)
        self.reader = self.socket.makefile("r", encoding="utf-8", newline="\n")

    def send(self, message: str) -> None:
        self.socket.sendall(f"{message}\n".encode())

    def receive(self) -> str:
        return self.reader.readline().rstrip("\r\n")

    def close(self) -> None:
        self.reader.close()
        self.socket.close()


class TalkIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = create_server("127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.clients: list[LineClient] = []

    def tearDown(self) -> None:
        for client in self.clients:
            client.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def connect(self) -> LineClient:
        client = LineClient(self.server.server_address[1])
        self.clients.append(client)
        return client

    def test_registration_messaging_listing_and_quit(self) -> None:
        alice = self.connect()
        bob = self.connect()

        alice.send("register Alice")
        self.assertEqual(alice.receive(), "Alice registered")
        bob.send("register Bob")
        self.assertEqual(bob.receive(), "Bob registered")

        alice.send("send_all hello everyone")
        self.assertEqual(alice.receive(), "message from Alice: hello everyone")
        self.assertEqual(bob.receive(), "message from Alice: hello everyone")

        alice.send("send_to Bob private hello")
        self.assertEqual(bob.receive(), "message from Alice: private hello")

        alice.send("user_list")
        self.assertEqual(alice.receive(), "Online users: Alice, Bob")

        alice.send("quit")
        self.assertEqual(alice.receive(), "Client exiting")

    def test_unknown_command_and_registration_requirement(self) -> None:
        client = self.connect()
        client.send("send_all hello")
        self.assertEqual(client.receive(), "not registered")
        client.send("not_a_command")
        self.assertEqual(client.receive(), "not registered")
        client.send("register Alice")
        self.assertEqual(client.receive(), "Alice registered")
        client.send("not_a_command")
        self.assertEqual(client.receive(), "unknown command")


if __name__ == "__main__":
    unittest.main()
