"""Handle the RADIUS socket (async-ready implementation)"""
import asyncio
import socket
from chewie.utils import get_logger

class RadiusSocket:
    def __init__(self, listen_ip, listen_port, server_ip, server_port, log_prefix):
        self.socket = None
        self.listen_ip = listen_ip
        self.listen_port = listen_port
        self.server_ip = server_ip
        self.server_port = server_port
        self.logger = get_logger("wired_8021x")
        self.loop = asyncio.get_event_loop()
        self._recv_future = None

    def setup(self):
        self.logger.debug("Setting up radius socket.")
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.socket.bind((self.listen_ip, self.listen_port))
            self.socket.setblocking(False)
        except socket.error as err:
            self.logger.error("Socket setup failed: %s", str(err))
            raise

    async def send(self, data: bytes) -> None:
        """Thread-safe sendto for older Python versions"""
        await self.loop.run_in_executor(
            None, 
            self.socket.sendto, 
            data, 
            (self.server_ip, self.server_port)
        )

    async def receive(self) -> bytes:
        """Async receive using event loop readability checks"""
        self._recv_future = self.loop.create_future()
        self.loop.add_reader(self.socket.fileno(), self._read_ready)
        try:
            return await self._recv_future
        finally:
            self.loop.remove_reader(self.socket.fileno())

    def _read_ready(self):
        """Callback when socket has data"""
        try:
            data, addr = self.socket.recvfrom(4096)
            self.logger.debug("Received RADIUS packet from %s:%d", addr[0], addr[1])
            if self._recv_future and not self._recv_future.done():
                self._recv_future.set_result(data)
        except BlockingIOError:
            self.logger.error("Blocking error")
        except Exception as e:
            self.logger.error("Read error: %s", str(e))
            if self._recv_future and not self._recv_future.done():
                self._recv_future.set_exception(e)

    def close(self):
        """Gracefully close the socket"""
        try:
            self.socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass  # UDP sockets don't support shutdown, but we still close
        self.socket.close()
        self.logger.debug("Closed RADIUS socket")
