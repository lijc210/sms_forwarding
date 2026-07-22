import asyncio
import re
import time
import logging
from typing import Callable, Awaitable

try:
    import serial_asyncio
except ImportError:
    import serial.aio as serial_asyncio

logger = logging.getLogger(__name__)


class ATError(Exception):
    def __init__(self, response: list[str], message: str = "AT command returned ERROR"):
        self.response = response
        super().__init__(f"{message}: {' / '.join(response[-3:])}")


def _is_unsolicited(line: str) -> bool:
    return any(line.startswith(p) for p in ["+CMTI:", "+CDS:", "+CUSD:", "+CMT:"])


class ATSerial:
    def __init__(self, port: str = "/dev/ttyUSB2", baudrate: int = 115200):
        self.port = port
        self.baudrate = baudrate
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._cmd_lock = asyncio.Lock()
        self._running = False
        self._line_queue: asyncio.Queue[str] = asyncio.Queue()
        self._reader_task: asyncio.Task | None = None
        self._handlers: list[Callable[[str], Awaitable[None]]] = []
        self.connected = False

    async def connect(self):
        self._reader, self._writer = await serial_asyncio.open_serial_connection(
            url=self.port, baudrate=self.baudrate
        )
        self._running = True
        self.connected = True
        self._reader_task = asyncio.create_task(self._read_lines())
        logger.info("Connected to %s at %d baud", self.port, self.baudrate)

    async def disconnect(self):
        self._running = False
        self.connected = False
        if self._reader_task:
            self._reader_task.cancel()
            self._reader_task = None
        if self._writer:
            self._writer.close()
            self._writer = None
        logger.info("Disconnected from %s", self.port)

    async def _read_lines(self):
        while self._running:
            try:
                line = await asyncio.wait_for(self._reader.readline(), timeout=0.5)
                decoded = line.decode(errors="replace").strip()
                if decoded:
                    await self._line_queue.put(decoded)
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.error("Serial read error: %s", e)
                self._running = False
                self.connected = False
                break

    async def send_command(self, command: str, timeout: float = 5) -> list[str]:
        async with self._cmd_lock:
            if not self._writer:
                raise ConnectionError("Serial port not connected")
            full_cmd = command if command.startswith("AT") else "AT" + command
            logger.debug(">>> %s", full_cmd)
            self._writer.write((full_cmd + "\r\n").encode())
            await self._writer.drain()

            response: list[str] = []
            start = time.monotonic()
            while time.monotonic() - start < timeout:
                try:
                    line = await asyncio.wait_for(self._line_queue.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue

                if line == "OK":
                    return response
                if line == "ERROR":
                    raise ATError(response)
                if line.startswith("+CME ERROR:"):
                    raise ATError(response, f"AT command returned {line}")
                if line.strip() == full_cmd.strip():
                    continue
                if _is_unsolicited(line):
                    for h in self._handlers:
                        asyncio.ensure_future(h(line))
                    continue
                response.append(line)

            raise ATError(response, f"Command {full_cmd} timed out")

    async def send_sms(self, number: str, text: str, timeout: float = 60) -> int:
        async with self._cmd_lock:
            if not self._writer:
                raise ConnectionError("Serial port not connected")

            cmd = f'AT+CMGS="{number}"'
            self._writer.write((cmd + "\r\n").encode())
            await self._writer.drain()

            start = time.monotonic()
            prompt_received = False
            while time.monotonic() - start < timeout:
                try:
                    line = await asyncio.wait_for(self._line_queue.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                if line == "> " or line == ">":
                    prompt_received = True
                    break
                if line == "ERROR":
                    raise ATError([], "SMS command rejected")
                if line.strip() == cmd.strip():
                    continue

            if not prompt_received:
                raise ATError([], "SMS prompt not received (timeout)")

            self._writer.write((text + "\x1a").encode())
            await self._writer.drain()

            response: list[str] = []
            while time.monotonic() - start < timeout:
                try:
                    line = await asyncio.wait_for(self._line_queue.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                if line == "OK":
                    for r in response:
                        m = re.search(r'\+CMGS:\s*(\d+)', r)
                        if m:
                            return int(m.group(1))
                    return 0
                if line == "ERROR":
                    raise ATError(response, "SMS send failed")
                response.append(line)

            raise ATError(response, "SMS send timed out")

    def add_handler(self, handler: Callable[[str], Awaitable[None]]):
        self._handlers.append(handler)
