from __future__ import annotations

import select
import socket
import subprocess
import sys
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "Talk"))
from myclient import ChatClient
from myserver import ChatHandler, create_server


class Peer:
    def __init__(self, port: int, receive_buffer: int | None = None) -> None:
        self.socket = socket.socket()
        self.socket.settimeout(2)
        if receive_buffer:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, receive_buffer)
        self.socket.connect(("127.0.0.1", port))
        self.buffer = bytearray()

    def send(self, message: str) -> None:
        self.socket.sendall(f"{message}\n".encode())

    def receive(self, timeout: float = 2) -> str:
        deadline = time.monotonic() + timeout
        while b"\n" not in self.buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("No complete chat response")
            ready, _, _ = select.select([self.socket], [], [], remaining)
            if not ready:
                raise TimeoutError("No chat response")
            try:
                chunk = self.socket.recv(65536)
            except ConnectionResetError:
                return ""
            if not chunk:
                return ""
            self.buffer.extend(chunk)
        line, _, remainder = self.buffer.partition(b"\n")
        self.buffer = bytearray(remainder)
        return line.decode().rstrip("\r")

    def close(self) -> None:
        self.socket.close()


class BoundedTalkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.servers = []
        self.clients: list[Peer] = []
        self.processes = []

    def tearDown(self) -> None:
        for process in self.processes:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=3)
        for peer in self.clients:
            peer.close()
        for server, thread in self.servers:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(server.diagnostics()["active_connections"], 0)
            self.assertEqual(server.diagnostics()["handler_threads"], 0)

    def start_server(self, **options):
        server = create_server("127.0.0.1", 0, **options)
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        thread.start()
        self.servers.append((server, thread))
        return server

    def peer(self, server, username=None, **options) -> Peer:
        peer = Peer(server.server_address[1], **options)
        self.clients.append(peer)
        if username:
            peer.send(f"register {username}")
            self.assertEqual(peer.receive(), f"{username} registered")
        return peer

    def eventually(self, predicate, timeout=2) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail("Condition was not reached within the deadline")

    def test_split_multibyte_and_coalesced_commands_keep_frames(self) -> None:
        server = self.start_server()
        alice = self.peer(server)
        encoded = "register 사용자\nsend_all 안녕하세요 세계\nuser_list\n".encode()
        # Split within a Korean UTF-8 character, not just between commands.
        split = len(b"register ") + 1
        alice.socket.sendall(encoded[:split])
        alice.socket.sendall(encoded[split : split + 1])
        alice.socket.sendall(encoded[split + 1 :])
        self.assertEqual(alice.receive(), "사용자 registered")
        self.assertEqual(alice.receive(), "message from 사용자: 안녕하세요 세계")
        self.assertEqual(alice.receive(), "Online users: 사용자")

    def test_simultaneous_name_registration_has_one_winner(self) -> None:
        server = self.start_server()
        peers = [self.peer(server), self.peer(server)]
        barrier = threading.Barrier(2)

        def register(peer):
            barrier.wait(timeout=2)
            peer.send("register Same")
            return peer.receive()

        with ThreadPoolExecutor(max_workers=2) as workers:
            responses = list(workers.map(register, peers))
        self.assertCountEqual(responses, ["Same registered", "username taken"])
        loser = peers[responses.index("username taken")]
        loser.send("register Other")
        self.assertEqual(loser.receive(), "Other registered")
        self.assertEqual(server.state.usernames(), ["Other", "Same"])

    def test_confirmation_precedes_concurrent_broadcast(self) -> None:
        server = self.start_server()
        alice = self.peer(server, "Alice")
        bob = self.peer(server)
        confirmation_entered = threading.Event()
        release_confirmation = threading.Event()
        broadcast_attempted = threading.Event()
        original_send = ChatHandler.send_message
        original_process = ChatHandler.process

        def intercept_send(handler, message):
            if message == "Bob registered":
                confirmation_entered.set()
                release_confirmation.wait(timeout=2)
            return original_send(handler, message)

        def intercept_process(handler, message):
            if message == "send_all during-registration":
                broadcast_attempted.set()
            return original_process(handler, message)

        with (
            patch.object(ChatHandler, "send_message", intercept_send),
            patch.object(ChatHandler, "process", intercept_process),
        ):
            try:
                bob.send("register Bob")
                self.assertTrue(confirmation_entered.wait(timeout=2))
                alice.send("send_all during-registration")
                self.assertTrue(broadcast_attempted.wait(timeout=2))
            finally:
                release_confirmation.set()
            self.assertEqual(bob.receive(), "Bob registered")
            self.assertEqual(bob.receive(), "message from Alice: during-registration")
            self.assertEqual(alice.receive(), "message from Alice: during-registration")

    def test_registration_deadline_reclaims_connection(self) -> None:
        server = self.start_server(registration_timeout=0.15)
        peer = self.peer(server)
        self.assertEqual(peer.receive(), "registration timeout")
        self.assertEqual(peer.receive(), "")
        self.eventually(lambda: server.snapshot()["active_connections"] == 0)
        self.assertEqual(server.snapshot()["registration_timeouts"], 1)

    def test_partial_input_does_not_extend_registration_deadline(self) -> None:
        server = self.start_server(registration_timeout=0.2)
        peer = self.peer(server)
        peer.socket.sendall(b"register unfinished")
        self.assertEqual(peer.receive(), "registration timeout")
        self.assertEqual(peer.receive(), "")

    def test_connection_limit_rejects_before_creating_extra_handler(self) -> None:
        server = self.start_server(max_clients=1)
        first = self.peer(server, "First")
        rejected = self.peer(server)
        self.assertEqual(rejected.receive(), "server busy")
        self.assertEqual(rejected.receive(), "")
        self.assertEqual(server.snapshot()["connections_rejected"], 1)
        self.assertEqual(server.snapshot()["handler_threads"], 1)
        first.send("quit")
        self.assertEqual(first.receive(), "Client exiting")
        self.eventually(lambda: server.snapshot()["handler_threads"] == 0)
        replacement = self.peer(server, "Replacement")
        replacement.send("user_list")
        self.assertEqual(replacement.receive(), "Online users: Replacement")

    def test_name_validation_counts_utf8_bytes_and_rejects_controls(self) -> None:
        server = self.start_server(max_name_bytes=6)
        peer = self.peer(server)
        for name, expected in [
            ("가나다", "username too long"),
            ("A B", "username must not contain spaces"),
            ("A\x1bB", "username contains control characters"),
            ("", "username required"),
        ]:
            peer.send(f"register {name}")
            self.assertEqual(peer.receive(), expected)
        peer.send("register 가나")
        self.assertEqual(peer.receive(), "가나 registered")
        peer.send("register New")
        self.assertEqual(peer.receive(), "already registered")

    def test_oversized_terminated_and_unterminated_frames_are_closed(self) -> None:
        server = self.start_server(max_line_bytes=64)
        for suffix in (b"", b"\n"):
            with self.subTest(suffix=suffix):
                peer = self.peer(server)
                peer.socket.sendall(b"x" * 65 + suffix)
                self.assertEqual(peer.receive(), "message too long")
                self.assertEqual(peer.receive(), "")
        self.assertEqual(server.snapshot()["oversized_frames"], 2)

    def test_exact_byte_limit_is_accepted(self) -> None:
        server = self.start_server(max_line_bytes=64)
        peer = self.peer(server, "Alice")
        text = "x" * (64 - len("send_all "))
        peer.send(f"send_all {text}")
        self.assertEqual(peer.receive(), f"message from Alice: {text}")
        self.assertEqual(server.snapshot()["oversized_frames"], 0)

    def test_invalid_utf8_is_rejected_without_changing_identity(self) -> None:
        server = self.start_server()
        peer = self.peer(server)
        peer.socket.sendall(b"register A\xff\n")
        self.assertEqual(peer.receive(), "invalid utf-8")
        self.assertEqual(peer.receive(), "")
        self.assertEqual(server.snapshot()["invalid_utf8_frames"], 1)
        self.assertEqual(server.state.usernames(), [])

    def test_abrupt_disconnect_releases_name_and_can_reconnect(self) -> None:
        server = self.start_server()
        first = self.peer(server, "Alice")
        first.socket.shutdown(socket.SHUT_RDWR)
        first.close()
        self.eventually(lambda: not server.state.usernames())
        second = self.peer(server, "Alice")
        second.send("user_list")
        self.assertEqual(second.receive(), "Online users: Alice")

    def test_invalid_command_arguments_preserve_original_responses(self) -> None:
        server = self.start_server()
        peer = self.peer(server, "Alice")
        for command, response in [
            ("send_all", "message required"),
            ("send_to", "Usage: send_to <username> <message>"),
            ("send_to Bob", "Usage: send_to <username> <message>"),
            ("send_to Bob hello", "User Bob not found"),
            ("not-a-command", "unknown command"),
        ]:
            peer.send(command)
            self.assertEqual(peer.receive(), response)
        peer.send("\n \nuser_list")
        self.assertEqual(peer.receive(), "Online users: Alice")

    def test_send_deadline_counts_failure_not_successful_delivery(self) -> None:
        server = self.start_server(
            max_line_bytes=131072,
            send_buffer_bytes=4096,
            send_timeout=0.15,
            outbound_queue_size=128,
        )
        slow = self.peer(server, "Slow", receive_buffer=1024)
        sender = self.peer(server, "Sender")
        self.eventually(lambda: server.snapshot()["messages_sent"] == 2)
        before = server.snapshot()
        for _ in range(32):
            sender.send("send_to Slow " + "x" * 65536)
        self.eventually(lambda: server.snapshot()["send_failures"] == 1)
        after = server.snapshot()
        self.assertGreater(after["messages_enqueued"], before["messages_enqueued"])
        self.assertLess(after["messages_sent"], after["messages_enqueued"])
        self.assertIsNone(server.state.find("Slow"))
        sender.send("user_list")
        for _ in range(33):
            response = sender.receive()
            if response == "Online users: Sender":
                break
            self.assertEqual(response, "User Slow not found")
        else:
            self.fail("Sender did not receive its list after the peer was removed")
        slow.close()

    def test_stalled_peer_does_not_block_healthy_broadcast_receivers(self) -> None:
        server = self.start_server(
            max_line_bytes=131072,
            send_buffer_bytes=4096,
            send_timeout=0.2,
            outbound_queue_size=8,
        )
        self.peer(server, "Slow", receive_buffer=1024)
        sender = self.peer(server, "Sender")
        healthy = self.peer(server, "Healthy")
        for number in range(20):
            text = f"{number}:" + "x" * 32768
            sender.send(f"send_all {text}")
            expected = f"message from Sender: {text}"
            self.assertEqual(sender.receive(), expected)
            self.assertEqual(healthy.receive(), expected)
        self.eventually(lambda: server.state.find("Slow") is None)
        self.assertGreaterEqual(
            server.snapshot()["queue_overflows"] + server.snapshot()["send_failures"], 1
        )
        self.assertEqual(server.state.usernames(), ["Healthy", "Sender"])

    def test_server_shutdown_wakes_registered_and_unregistered_connections(
        self,
    ) -> None:
        server = self.start_server()
        peers = [self.peer(server, "Alice"), self.peer(server)]
        started = time.monotonic()
        server.shutdown()
        server.server_close()
        self.assertLess(time.monotonic() - started, 2)
        for peer in peers:
            self.assertEqual(peer.receive(), "")
        self.assertEqual(server.snapshot()["handler_threads"], 0)

    def test_repeated_start_and_teardown_leaves_no_connection_threads(self) -> None:
        for iteration in range(5):
            server = self.start_server()
            self.peer(server, f"User{iteration}")
            server.shutdown()
            server.server_close()
            self.assertEqual(server.snapshot()["handler_threads"], 0)
            self.assertEqual(server.snapshot()["active_connections"], 0)

    def cli(self, server):
        process = subprocess.Popen(
            [
                sys.executable,
                "-B",
                "-u",
                str(ROOT / "Talk" / "myclient.py"),
                "127.0.0.1",
                str(server.server_address[1]),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"},
        )
        self.processes.append(process)
        return process

    def test_actual_cli_registers_sends_lists_and_quits(self) -> None:
        server = self.start_server()
        process = self.cli(server)
        output, _ = process.communicate(
            "Alice\nsend_all hello\nuser_list\nquit\n", timeout=5
        )
        self.assertEqual(process.returncode, 0, output)
        self.assertIn("Alice registered", output)
        self.assertIn("message from Alice: hello", output)
        self.assertIn("Online users: Alice", output)
        self.assertIn("Client exiting", output)
        self.assertNotIn("Traceback", output)

    def test_actual_cli_exits_when_server_stops_while_console_input_is_blocked(
        self,
    ) -> None:
        server = self.start_server()
        process = self.cli(server)
        process.stdin.write("Alice\n")
        process.stdin.flush()
        self.eventually(lambda: server.state.usernames() == ["Alice"])
        server.shutdown()
        server.server_close()
        # Keep stdin open: server disconnect itself must terminate the CLI.
        process.wait(timeout=3)
        output, _ = process.communicate(timeout=1)
        self.assertEqual(process.returncode, 0, output)
        self.assertNotIn("Traceback", output)

    def test_actual_cli_exits_on_registration_timeout_without_console_input(
        self,
    ) -> None:
        server = self.start_server(registration_timeout=0.5)
        process = self.cli(server)
        process.wait(timeout=3)
        output, _ = process.communicate(timeout=1)
        self.assertEqual(process.returncode, 0, output)
        self.assertIn("registration timeout", output)

    def test_actual_cli_eof_before_registration_exits_promptly(self) -> None:
        server = self.start_server()
        process = self.cli(server)
        output, _ = process.communicate("", timeout=3)
        self.assertEqual(process.returncode, 0, output)
        self.assertNotIn("Traceback", output)

    def test_actual_cli_retries_invalid_name_and_preserves_unicode(self) -> None:
        server = self.start_server()
        process = self.cli(server)
        output, _ = process.communicate(
            "Invalid Name\n사용자\nsend_all 안녕 🌍\nquit\n", timeout=5
        )
        self.assertEqual(process.returncode, 0, output)
        self.assertIn("username must not contain spaces", output)
        self.assertIn("사용자 registered", output)
        self.assertIn("message from 사용자: 안녕 🌍", output)
        self.assertIn("Client exiting", output)
        self.assertNotIn("Traceback", output)

    def test_client_close_wakes_actual_receiver_without_reader_lock_deadlock(
        self,
    ) -> None:
        server = self.start_server()
        client = ChatClient("127.0.0.1", server.server_address[1])
        client.connect()
        receiver = threading.Thread(target=client.receive_messages)
        client._receiver = receiver
        receiver.start()
        started = time.monotonic()
        client.close()
        receiver.join(timeout=1)
        self.assertFalse(receiver.is_alive())
        self.assertLess(time.monotonic() - started, 1)

    def test_invalid_limits_are_rejected_before_binding(self) -> None:
        for options in (
            {"max_clients": 0},
            {"max_line_bytes": -1},
            {"outbound_queue_size": True},
            {"send_timeout": 0},
            {"registration_timeout": float("nan")},
            {"max_name_bytes": 0},
        ):
            with self.subTest(options=options), self.assertRaises(ValueError):
                create_server("127.0.0.1", 0, **options)


if __name__ == "__main__":
    unittest.main()
