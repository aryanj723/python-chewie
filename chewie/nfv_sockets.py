"""Supplicant-Facing Sockets – asyncio version"""

import asyncio
import struct
from abc import ABC, abstractmethod
from fcntl import ioctl

from chewie.helper import socket           # std-lib socket re-export
from chewie.mac_address import MacAddress
from chewie.utils import get_logger


class PromiscuousSocket(ABC):
    """Abstract Raw Socket in Promiscuous Mode"""
    SIOCGIFINDEX = 0x8933
    PACKET_MR_PROMISC = 1
    SOL_PACKET = 263
    PACKET_ADD_MEMBERSHIP = 1
    EAP_ADDRESS = MacAddress.from_string("01:80:c2:00:00:03")

    @abstractmethod
    def send(self, data):  # pylint: disable=missing-docstring
        pass

    @abstractmethod
    def receive(self):  # pylint: disable=missing-docstring
        pass

    @abstractmethod
    def setup(self):  # pylint: disable=missing-docstring
        pass

    def __init__(self, interface_name, log_prefix):
        self.socket = None
        self.interface_index = None
        self.interface_name = interface_name
        self.logger = get_logger("wired_8021x")

    def _setup(self, socket_filter):
        """Set up the socket"""
        self.logger.debug("Setting up socket on interface: %s", self.interface_name)
        try:
            self.open(socket_filter)
            self.get_interface_index()
            self.set_interface_promiscuous()
        except socket.error as err:
            self.logger.error("Unable to setup socket: %s", str(err))
            raise err

    def open(self, socket_filter):
        """Setup EAP socket"""
        self.socket = socket.socket(socket.PF_PACKET, socket.SOCK_RAW, socket_filter)
        self.socket.bind((self.interface_name, 0))
        self.socket.setblocking(False)               # critical for asyncio

    def get_interface_index(self):
        """Get the interface index of the EAP Socket"""
        # http://man7.org/linux/man-pages/man7/netdevice.7.html
        request = struct.pack('16sI', self.interface_name.encode("utf-8"), 0)
        response = ioctl(self.socket, self.SIOCGIFINDEX, request)
        _ifname, self.interface_index = struct.unpack('16sI', response)

    def set_interface_promiscuous(self):
        """Sets the EAP interface to be able to receive EAP messages"""
        request = struct.pack("IHH8s", self.interface_index, self.PACKET_MR_PROMISC,
                              len(self.EAP_ADDRESS.address), self.EAP_ADDRESS.address)
        self.socket.setsockopt(self.SOL_PACKET, self.PACKET_ADD_MEMBERSHIP, request)


class EapSocket(PromiscuousSocket):
    """Handle the EAP socket"""

    def setup(self):
        """Set up the socket"""
        self._setup(socket.htons(0x888e))

    async def send(self, data: bytes) -> None:
        """Transmit data on the EAP socket."""
        loop = asyncio.get_running_loop()
        await loop.sock_sendall(self.socket, data)

    async def receive(self) -> bytes:
        """Await one EAP frame."""
        loop = asyncio.get_running_loop()
        return await loop.sock_recv(self.socket, 4096)
    
    def close(self):
        """Gracefully close the socket and remove from event loop if needed."""
        if self.socket is None:
            return
        try:
            self.socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass  # Socket already closed or not connected
        try:
            self.socket.close()
        except OSError:
            pass  # Already closed
        self.socket = None


class MabSocket(PromiscuousSocket):
    """Handle the Mab socket for DHCP Requests"""
    IP_ETHERTYPE = 0x0800
    DHCP_UDP_SRC = 68
    DHCP_UDP_DST = 67
    UDP_IPTYPE = b'\x11'

    def setup(self):
        """Configure socket for IP ethertype (0x0800)."""
        self._setup(socket.htons(self.IP_ETHERTYPE))

    def send(self, data):
        """Not Implemented -- This socket is purely for Listening"""
        raise NotImplementedError('Attempted to send data down the activity socket')

    async def receive(self):
        """Await DHCP-request frames only."""
        loop = asyncio.get_running_loop()
        while True:
            pkt = await loop.sock_recv(self.socket, 4096)

            # Filter: IPv4 + UDP + DHCP client→server ports
            if pkt[23:24] == self.UDP_IPTYPE:
                src_port = struct.unpack('>H', pkt[34:36])[0]
                dst_port = struct.unpack('>H', pkt[36:38])[0]
                if src_port == self.DHCP_UDP_SRC and dst_port == self.DHCP_UDP_DST:
                    return pkt
