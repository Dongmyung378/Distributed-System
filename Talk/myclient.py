#!/usr/bin/env python3
"""Interactive client for the original Talk newline protocol."""

from __future__ import annotations

import argparse
import codecs
import os
import queue
import select
import socket
import sys
import threading
import time


class ConsoleReader:
    """Poll stdin without leaving a blocked TextIO read at interpreter shutdown."""

    def __init__(self) -> None:
        self.fd = sys.stdin.fileno()
        self.buffer = ""
        self.eof = False
        self.decoder = codecs.getincrementaldecoder(sys.stdin.encoding or "utf-8")(
            errors="replace"
        )
        self.console = os.name == "nt" and sys.stdin.isatty()
        if os.name == "nt" and not self.console:
            import ctypes
            import msvcrt
            from ctypes import wintypes

            self.handle = msvcrt.get_osfhandle(self.fd)
            self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            self.kernel32.GetFileType.argtypes = [wintypes.HANDLE]
            self.kernel32.GetFileType.restype = wintypes.DWORD
            self.pipe = self.kernel32.GetFileType(self.handle) == 3
            self.kernel32.PeekNamedPipe.argtypes = [
                wintypes.HANDLE,
                wintypes.LPVOID,
                wintypes.DWORD,
                ctypes.POINTER(wintypes.DWORD),
                ctypes.POINTER(wintypes.DWORD),
                ctypes.POINTER(wintypes.DWORD),
            ]
            self.kernel32.PeekNamedPipe.restype = wintypes.BOOL

    def _read_available(self) -> bytes | None:
        if os.name == "nt":
            if self.pipe:
                import ctypes
                from ctypes import wintypes

                available = wintypes.DWORD()
                if not self.kernel32.PeekNamedPipe(
                    self.handle, None, 0, None, ctypes.byref(available), None
                ):
                    error = ctypes.get_last_error()
                    if error in (6, 109, 232):
                        return b""
                    raise OSError(error, "Cannot read console input pipe")
                if not available.value:
                    return None
                return os.read(self.fd, min(4096, available.value))
            return os.read(self.fd, 4096)
        readable, _, _ = select.select([self.fd], [], [], 0.05)
        return os.read(self.fd, 4096) if readable else None

    def readline(self, running: threading.Event) -> str | None:
        if self.console:
            import msvcrt

            characters = []
            high_surrogate = ""
            while running.is_set():
                if not msvcrt.kbhit():
                    time.sleep(0.02)
                    continue
                character = msvcrt.getwch()
                if 0xD800 <= ord(character) <= 0xDBFF:
                    high_surrogate = character
                    continue
                if high_surrogate:
                    character = (
                        (high_surrogate + character)
                        .encode("utf-16-le", errors="surrogatepass")
                        .decode("utf-16-le", errors="replace")
                    )
                    high_surrogate = ""
                if character in ("\x00", "\xe0"):
                    msvcrt.getwch()
                elif character in ("\r", "\n"):
                    print(flush=True)
                    return "".join(characters)
                elif character in ("\x03", "\x1a"):
                    return None
                elif character == "\b":
                    if characters:
                        characters.pop()
                        print("\b \b", end="", flush=True)
                else:
                    characters.append(character)
                    print(character, end="", flush=True)
            return None
        while running.is_set():
            if "\n" in self.buffer:
                line, _, self.buffer = self.buffer.partition("\n")
                return line.rstrip("\r")
            if self.eof:
                line, self.buffer = self.buffer, ""
                return line or None
            chunk = self._read_available()
            if chunk is None:
                time.sleep(0.01)
            elif not chunk:
                self.eof = True
                self.buffer += self.decoder.decode(b"", final=True)
            else:
                self.buffer += self.decoder.decode(chunk)
        return None


class ChatClient:
    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.socket: socket.socket | None = None
        self.running = threading.Event()
        self._buffer = bytearray()
        self._send_lock = threading.Lock()
        self._messages: queue.Queue[str] = queue.Queue(maxsize=128)
        self._input_requests: queue.Queue[str | None] = queue.Queue(maxsize=1)
        self._inputs: queue.Queue[str | None] = queue.Queue(maxsize=1)
        self._receiver: threading.Thread | None = None
        self._console: threading.Thread | None = None

    def connect(self) -> None:
        self.socket = socket.create_connection((self.host, self.port), timeout=10)
        self.socket.setblocking(False)
        self.running.set()
        print(f"Connected to {self.host}:{self.port}", flush=True)

    def send(self, message: str) -> None:
        connection = self.socket
        if connection is None:
            raise RuntimeError("Client is not connected")
        frame = f"{message.strip()}\n".encode()
        with self._send_lock:
            remaining = memoryview(frame)
            deadline = time.monotonic() + 5
            while remaining and self.running.is_set():
                wait = deadline - time.monotonic()
                if wait <= 0:
                    raise TimeoutError("Send timed out")
                _, writable, _ = select.select([], [connection], [], min(wait, 0.1))
                if writable:
                    try:
                        sent = connection.send(remaining)
                    except BlockingIOError:
                        continue
                    if not sent:
                        raise ConnectionError("Server closed the connection")
                    remaining = remaining[sent:]

    def read_message(self) -> str | None:
        connection = self.socket
        if connection is None:
            return None
        while self.running.is_set():
            if b"\n" in self._buffer:
                line, _, remainder = self._buffer.partition(b"\n")
                self._buffer = bytearray(remainder)
                if len(line) > 1024 * 1024:
                    raise ConnectionError("Server response exceeded the client limit")
                return line.decode("utf-8").rstrip("\r")
            if len(self._buffer) > 1024 * 1024:
                raise ConnectionError("Server response exceeded the client limit")
            readable, _, _ = select.select([connection], [], [], 0.1)
            if not readable:
                continue
            try:
                chunk = connection.recv(4096)
            except BlockingIOError:
                continue
            if not chunk:
                return None
            self._buffer.extend(chunk)
        return None

    def receive_messages(self) -> None:
        try:
            while self.running.is_set():
                message = self.read_message()
                if message is None:
                    break
                while self.running.is_set():
                    try:
                        self._messages.put(message, timeout=0.1)
                        break
                    except queue.Full:
                        continue
                if message == "Client exiting":
                    break
        except (OSError, ValueError, UnicodeDecodeError):
            pass
        finally:
            self.running.clear()

    def _read_console(self) -> None:
        reader = ConsoleReader()
        while self.running.is_set():
            try:
                prompt = self._input_requests.get(timeout=0.1)
            except queue.Empty:
                continue
            if prompt is None:
                return
            print(prompt, end="", flush=True)
            try:
                result = reader.readline(self.running)
            except OSError:
                result = None
            while self.running.is_set():
                try:
                    self._inputs.put(result, timeout=0.1)
                    break
                except queue.Full:
                    continue

    def _next_input(self, prompt: str = "") -> str | None:
        self._input_requests.put_nowait(prompt)
        while self.running.is_set():
            self._display_messages()
            try:
                return self._inputs.get(timeout=0.05)
            except queue.Empty:
                continue
        return None

    def _display_messages(self) -> None:
        while True:
            try:
                print(self._messages.get_nowait(), flush=True)
            except queue.Empty:
                return

    def register(self) -> bool:
        while self.running.is_set():
            username = self._next_input("Screen name: ")
            if username is None:
                return False
            username = username.strip()
            self.send(f"register {username}")
            while self.running.is_set() or not self._messages.empty():
                try:
                    response = self._messages.get(timeout=0.05)
                except queue.Empty:
                    continue
                print(response, flush=True)
                if response == f"{username} registered":
                    return True
                if not response.startswith("message from "):
                    break
        return False

    def run(self) -> None:
        self.connect()
        try:
            self._receiver = threading.Thread(
                target=self.receive_messages, name="talk-receiver", daemon=True
            )
            console = threading.Thread(
                target=self._read_console, name="talk-console", daemon=True
            )
            self._console = console
            self._receiver.start()
            console.start()
            if not self.register():
                self._display_messages()
                return
            while self.running.is_set():
                message = self._next_input()
                if not self.running.is_set():
                    break
                if message is None:
                    message = "quit"
                if not message.strip():
                    continue
                self.send(message)
                if message.strip() == "quit":
                    deadline = time.monotonic() + 5
                    while self.running.is_set() and time.monotonic() < deadline:
                        self._display_messages()
                        time.sleep(0.01)
                    break
            self._display_messages()
        finally:
            self.close()

    def close(self) -> None:
        self.running.clear()
        connection, self.socket = self.socket, None
        if connection is not None:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
        if self._receiver and self._receiver is not threading.current_thread():
            self._receiver.join(timeout=1)
        try:
            self._input_requests.put_nowait(None)
        except queue.Full:
            pass
        if self._console and self._console is not threading.current_thread():
            self._console.join(timeout=1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host", nargs="?", default="127.0.0.1")
    parser.add_argument("port", nargs="?", type=int, default=8090)
    args = parser.parse_args()
    try:
        ChatClient(args.host, args.port).run()
    except (ConnectionError, OSError, KeyboardInterrupt, ValueError) as error:
        print(f"Connection ended: {error}", flush=True)


if __name__ == "__main__":
    main()
