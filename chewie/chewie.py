""" Entry point for 802.1X speaker. """
from chewie.helper import sleep, GreenPool, Queue
from chewie import timer_scheduler
from chewie.eap import Eap
from chewie.nfv_sockets import EapSocket, MabSocket
from chewie.ethernet_packet import EthernetPacket
from chewie.event import EventMessageReceived, EventPortStatusChange, \
    EventPreemptiveEAPResponseMessageReceived
from chewie.mac_address import MacAddress
from chewie.message_parser import MessageParser, MessagePacker, IdentityMessage
from chewie.radius_lifecycle import RadiusLifecycle
from chewie.radius_socket import RadiusSocket
from chewie.state_machines.eap_state_machine import FullEAPStateMachine
from chewie.state_machines.mab_state_machine import MacAuthenticationBypassStateMachine
from chewie.utils import get_logger, MessageParseError, EapQueueMessage
from chewie.utils import get_random_id as utils_get_random_id
import asyncio

def get_random_id():
    return utils_get_random_id()


def unpack_byte_string(byte_string):
    """unpacks a byte string"""
    return "".join("%02x" % x for x in byte_string)


# TODO set unneeded public methods to private
# pylint: disable=too-many-instance-attributes
# pylint: disable=too-many-public-methods
class Chewie:
    """Facilitates EAP supplicant and RADIUS server communication"""
    RADIUS_UDP_PORT = 1812
    PAE_GROUP_ADDRESS = MacAddress.from_string("01:80:C2:00:00:03")

    DEFAULT_PORT_UP_IDENTITY_REQUEST_WAIT_PERIOD = 20
    DEFAULT_PREEMPTIVE_IDENTITY_REQUEST_INTERVAL = 60

    # pylint: disable=too-many-arguments
    def __init__(self, interface_name, logger=None,
                 auth_handler=None, failure_handler=None, logoff_handler=None,
                 radius_server_ip=None, radius_server_port=None, radius_server_secret=None,
                 chewie_id=None):

        self.interface_name = interface_name
        self.log_name = Chewie.__name__
        if logger:
            self.log_name = logger.name + "." + Chewie.__name__

        self.logger = get_logger("wired_8021x")
        self.auth_handler = auth_handler
        self.failure_handler = failure_handler
        self.logoff_handler = logoff_handler

        self.radius_server_ip = radius_server_ip
        self.radius_secret = radius_server_secret
        self.radius_server_port = self.RADIUS_UDP_PORT
        if radius_server_port:
            self.radius_server_port = radius_server_port
        self.radius_listen_ip = "0.0.0.0"
        self.radius_listen_port = 0

        self.chewie_id = "44-44-44-44-44-44:"  # used by the RADIUS Attribute
        # 'Called-Station' in Access-Request
        if chewie_id:
            self.chewie_id = chewie_id

        self.state_machines = {}  # port_id_str: { mac : state_machine}
        self.port_to_eapol_id = {}  # port_id: last ID used in preemptive identity request.
        # TODO for port_to_eapol_id - may want to set ID to null (-1...) if sent from the
        #  state machine.
        self.port_status = {}  # port_id: status (true=up, false=down)
        self.port_to_identity_job = {}  # port_id: timerJob

        self.eap_output_messages = Queue()
        self.radius_output_messages = Queue()

        self.radius_lifecycle = RadiusLifecycle(self.radius_secret, self.chewie_id, self.logger)
        self.timer_scheduler = timer_scheduler.TimerScheduler(self.logger)

        self.eap_socket = None
        self.mab_socket = None
        self.pool = None
        self.eventlets = None
        self.radius_socket = None
        self.interface_index = None

        self.eventlets = []

    async def run(self):
        """Main async entry point"""
        self.logger.debug("Starting Chewie service")
        self.running = True  # Add this flag
    
        try:
            self.setup_eap_socket()
            self.setup_mab_socket()
            self.setup_radius_socket()
            await self.start_threads_and_wait()
        except asyncio.CancelledError:
            self.logger.debug("Chewie shutdown initiated")
        finally:
            self.cleanup()

    def cleanup(self):
        """Close all sockets"""
        self.logger.debug("Cleaning up sockets")
        self.running = False  # Break receive loops
        if self.eap_socket:
            self.eap_socket.close()
        if self.radius_socket:
            self.radius_socket.close()


    def shutdown(self):
        """Cancel all eventlet/GreenPool tasks."""
        for eventlet in self.eventlets:
            eventlet.cancel()

    async def stop(self):
        """Async-compatible, orderly shutdown."""
        self.running = False
        await asyncio.sleep(0.25)  # Give tasks a chance to notice
        self.shutdown()            # Cancel all tasks
    
    async def start_threads_and_wait(self):
        """Start the thread and wait until they complete (hopefully never)"""
        self.pool = GreenPool()

        self.eventlets.append(self.pool.spawn(self.send_eap_messages))
        self.eventlets.append(self.pool.spawn(self.receive_eap_messages))
        self.eventlets.append(self.pool.spawn(self.receive_mab_messages))

        self.eventlets.append(self.pool.spawn(self.send_radius_messages))
        self.eventlets.append(self.pool.spawn(self.receive_radius_messages))

        self.eventlets.append(self.pool.spawn(self.timer_scheduler.run))

        await self.pool.waitall()

    def auth_success(self, src_mac, port_id, period,
                     *args, **kwargs):  # pylint: disable=unused-variable
        """authentication shim between faucet and chewie
        Args:
            src_mac (MacAddress): the mac of the successful supplicant
            port_id (MacAddress): the 'mac' identifier of what switch port the success is on
            period (int): time (seconds) until the session times out.
            """

        if self.auth_handler:
            self.auth_handler(src_mac, port_id, *args, **kwargs)

        self.port_to_identity_job[port_id] = self.timer_scheduler.call_later(
            period,
            self.reauth_port, src_mac,
            port_id)

    def auth_failure(self, src_mac, port_id):
        """failure shim between faucet and chewie
        Args:
            src_mac (MacAddress): the mac of the failed supplicant
            port_id (MacAddress): the 'mac' identifier of what switch port
             the failure is on"""
        if self.failure_handler:
            self.failure_handler(src_mac, port_id)

    def auth_logoff(self, src_mac, port_id):
        """logoff shim between faucet and chewie
        Args:
            src_mac (MacAddress): the mac of the logoff supplicant
            port_id (MacAddress): the 'mac' identifier of what switch port
             the logoff is on"""
        if self.logoff_handler:
            self.logoff_handler(src_mac, port_id)

    def port_down(self, port_id):
        """
        should be called by faucet when port has gone down.
        Args:
            port_id (str): id of port.
        """
        # all chewie needs to do is change its internal state.
        # faucet will remove the acls by itself.
        self.set_port_status(port_id, False)

        job = self.port_to_identity_job.get(port_id, None)

        if port_id in self.state_machines:
            del self.state_machines[port_id]

        if job:
            job.cancel()
        self.port_to_eapol_id.pop(port_id, None)

    def port_up(self, port_id):
        """
        should be called by faucet when port has come up
        Args:
            port_id (str): id of port.
        """
        self.logger.debug("port %s up", port_id)
        self.set_port_status(port_id, True)

        self.port_to_identity_job[port_id] = self.timer_scheduler.call_later(
            self.DEFAULT_PORT_UP_IDENTITY_REQUEST_WAIT_PERIOD,
            self.send_preemptive_identity_request_if_no_active_on_port,
            port_id)

    def send_preemptive_identity_request_if_no_active_on_port(self, port_id):
        """
        If there is no active (in progress, or in state success(2)) supplicant send out the
        preemptive identity request message.
        Args:
            port_id (str):
        """
        self.logger.debug("thinking about executing timer preemptive on port %s", port_id)
        # schedule next request.
        self.port_to_identity_job[port_id] = self.timer_scheduler.call_later(
            self.DEFAULT_PREEMPTIVE_IDENTITY_REQUEST_INTERVAL,
            self.send_preemptive_identity_request_if_no_active_on_port,
            port_id)
        if not self.port_status.get(port_id, False):
            self.logger.debug('cant send output on port %s is down', port_id)
            return

        state_machines = self.state_machines.get(port_id, {})

        # pylint: disable=invalid-name
        for sm in state_machines.values():
            if sm.is_in_progress() or sm.is_success():
                self.logger.debug('port is active not sending on port %s', port_id)
                break
        else:
            self.logger.debug("executing timer premptive on port %s", port_id)
            self.send_preemptive_identity_request(port_id)

    def send_preemptive_identity_request(self, port_id, state_machine=None):
        """
        Message (EAP Identity Request) that notifies supplicant that port is using 802.1X
        Args:
            port_id (str):

        """
        _id = get_random_id()
        # ID of preemptive reauth attempt must be different to ID of initial authentication.
        if state_machine is not None and hasattr(state_machine, 'current_id'):
            while _id == state_machine.current_id:
                _id = get_random_id()
        data = IdentityMessage(self.PAE_GROUP_ADDRESS, _id, Eap.REQUEST, "")
        self.port_to_eapol_id[port_id] = _id
        self.eap_output_messages.put_nowait(
            EapQueueMessage(data, self.PAE_GROUP_ADDRESS, MacAddress.from_string(port_id)))
        self.logger.debug("sending premptive on port %s with ID %s", port_id, _id)

    def reauth_port(self, src_mac, port_id):
        """
        Send an Identity Request to src_mac, on port_id. prompting
        the supplicant to re authenticate.
        Args:
            src_mac (MacAddress):
            port_id (str):
        """
        state_machine = self.state_machines.get(port_id, {}).get(str(src_mac), None)

        if state_machine and state_machine.is_success():
            self.logger.debug('reauthenticating src_mac: %s on port: %s', src_mac, port_id)
            self.send_preemptive_identity_request(port_id, state_machine)
        elif state_machine is None:
            self.logger.debug('not reauthing. state machine on port: %s, mac: %s is none', port_id,
                              src_mac)
        else:
            self.logger.debug("not reauthing, authentication is not in success(2) (state: %s)'",
                              state_machine.state)

    def set_port_status(self, port_id, status):
        """
        Send status of a port at port_id
        Args:
            port_id ():
            status ():
        """
        port_id_str = str(port_id)

        self.port_status[port_id] = status

        if port_id_str not in self.state_machines:
            self.state_machines[port_id_str] = {}

        for _, state_machine in self.state_machines[port_id_str].items():
            event = EventPortStatusChange(status)
            state_machine.event(event)

    def setup_eap_socket(self):
        """Setup EAP socket"""
        log_prefix = "%s.EapSocket" % self.logger.name
        self.eap_socket = EapSocket(self.interface_name, log_prefix)
        self.eap_socket.setup()

    def setup_mab_socket(self):
        """Setup Mab socket"""
        log_prefix = "%s.MabSocket" % self.logger.name
        self.mab_socket = MabSocket(self.interface_name, log_prefix)
        self.mab_socket.setup()

    def setup_radius_socket(self):
        """Setup Radius socket"""
        log_prefix = "%s.RadiusSocket" % self.logger.name
        self.radius_socket = RadiusSocket(self.radius_listen_ip,
                                          self.radius_listen_port,
                                          self.radius_server_ip,
                                          self.radius_server_port,
                                          log_prefix)
        self.radius_socket.setup()
        self.logger.debug("Radius Listening on %s:%d",
                         self.radius_listen_ip,
                         self.radius_listen_port)

    async def send_eap_messages(self):
        """Send EAP messages to Supplicant forever."""
        while self.running:
            await sleep(0)
            eap_queue_message = await self.eap_output_messages.get()
            self.logger.debug("Sending message %s from %s to %s",
                             eap_queue_message.message,
                             str(eap_queue_message.port_mac),
                             str(eap_queue_message.src_mac))
            await self.eap_socket.send(MessagePacker.ethernet_pack(eap_queue_message.message,
                                                             eap_queue_message.port_mac,
                                                             eap_queue_message.src_mac))

    def send_eth_to_state_machine(self, packed_message):
        """Send an ethernet frame to MAB State Machine"""
        ethernet_packet = EthernetPacket.parse(packed_message)
        port_id = ethernet_packet.dst_mac
        src_mac = ethernet_packet.src_mac

        self.logger.debug("Sending MAC to MAB State Machine: %s", src_mac)
        message_id = -2
        state_machine = self.get_state_machine(src_mac, port_id, message_id)
        event = EventMessageReceived(ethernet_packet, port_id)
        state_machine.event(event)
        # NOTE: Should probably throttle packets in once one is received

    async def receive_eap_messages(self):
        """Enhanced EAP receiver with proper shutdown handling"""
        try:
            while self.running:
                if not self.eap_socket or not self.eap_socket.socket:  # Critical check
                    self.logger.debug("Socket terminated during receive")
                    break
                packed_message = await self.eap_socket.receive()
                self.logger.debug("Received packed_message: %s", str(packed_message))
                
                try:
                    eap, dst_mac = MessageParser.ethernet_parse(packed_message)
                except MessageParseError as exc:
                    self.logger.warning("Parse error: %s", exc)
                    continue
                except OSError as e:
                    if e.errno == 100:  # Network down during shutdown
                        self.logger.debug("Network termination signal received")
                        break
                    raise
                
                self.send_eap_to_state_machine(eap, dst_mac)
        except OSError as e:
            if e.errno == 100:
                self.logger.debug("Network termination confirmed")       
        except asyncio.CancelledError:
            self.logger.info("Receiver task cancellation completed")
        finally:
            # Ensure socket closure even on unexpected exits
            if hasattr(self.eap_socket, 'close'):
                self.eap_socket.close()
            self.logger.debug("EAP receiver fully terminated")

    async def receive_mab_messages(self):
        """Receive DHCP request for MAB."""
        while self.running:
            await sleep(0)
            self.logger.debug("waiting for MAB activity.")
            try:
                packed_message = await self.mab_socket.receive()
                self.logger.debug("Received DHCP packet for MAB. packed_message: %s",
                                str(packed_message))
                self.send_eth_to_state_machine(packed_message)
            except:
                await sleep(2)
                if self.running:
                    self.logger.error("Error on receiving EAP message")
                else:
                    pass

    def send_eap_to_state_machine(self, eap, dst_mac):
        if not self.running:  # Essential check
            self.logger.debug("Dropping message during shutdown")
            return
        """sends an eap message to the state machine"""
        self.logger.debug("eap EAP(): %s", eap)
        message_id = getattr(eap, 'message_id', -1)
        state_machine = self.get_state_machine(eap.src_mac, dst_mac, message_id)

        # Check for response to preemptive_eap
        preemptive_eap_message_id = self.port_to_eapol_id.get(str(dst_mac), -2)
        if message_id != -1 and message_id == preemptive_eap_message_id:
            self.logger.debug('eap packet is response to chewie initiated authentication')
            event = EventPreemptiveEAPResponseMessageReceived(eap, dst_mac,
                                                              preemptive_eap_message_id)
        else:
            event = EventMessageReceived(eap, dst_mac)

        state_machine.event(event)

    async def send_radius_messages(self):
        """send RADIUS messages to RADIUS Server forever."""
        while self.running:
            await sleep(0)
            radius_output_bits = await self.radius_output_messages.get()
            packed_message = self.radius_lifecycle.process_outbound(radius_output_bits)
            await self.radius_socket.send(packed_message)
            self.logger.debug("sent radius message.")

    async def receive_radius_messages(self):
        """receive RADIUS messages from RADIUS server forever."""
        while self.running:
            await sleep(0)
            self.logger.debug("waiting for radius.")
            packed_message = await self.radius_socket.receive()
            try:
                radius = MessageParser.radius_parse(packed_message, self.radius_secret,
                                                    self.radius_lifecycle)
            except MessageParseError as exception:
                self.logger.warning(
                    "MessageParser.radius_parse threw exception.\n"
                    " packed_message: '%s'.\n"
                    " exception: '%s'.",
                    packed_message,
                    exception)
                continue
            self.logger.debug("Received RADIUS message: %s", str(radius))
            self.send_radius_to_state_machine(radius)

    def send_radius_to_state_machine(self, radius):
        """sends a radius message to the state machine"""
        event = self.radius_lifecycle.build_event_radius_message_received(radius)
        state_machine = self.get_state_machine_from_radius_packet_id(radius.packet_id)
        state_machine.event(event)

    def get_state_machine_from_radius_packet_id(self, packet_id):
        """Gets a FullEAPStateMachine from the RADIUS message packet_id
        Args:
            packet_id (int): id of the received RADIUS message
        Returns:
            FullEAPStateMachine
        """
        return self.get_state_machine(**self.radius_lifecycle.packet_id_to_mac[packet_id])

    # TODO change message_id functionality
    def get_state_machine(self, src_mac, port_id, message_id=-1):
        """Gets or creates if it does not already exist an FullEAPStateMachine for the src_mac.
        Args:
            message_id (int): eap message id, -1 means none found.
            src_mac (MacAddress): who's to get.
            port_id (MacAddress): ID of the port where the src_mac is.

        Returns:
            FullEAPStateMachine
        """
        port_id_str = str(port_id)
        src_mac_str = str(src_mac)
        port_state_machines = self.state_machines.get(port_id_str, None)
        if port_state_machines is None:
            self.state_machines[port_id_str] = {}

        self.logger.debug("Port based state machines are as follows: %s",
                         self.state_machines[port_id_str])
        state_machine = self.state_machines[port_id_str].get(src_mac_str, None)

        if not state_machine and message_id == -2:
            # Do MAB
            self.logger.debug("Creating MAB State Machine")
            log_prefix = "%s.SM - port: %s, client: %s" % (self.logger.name, port_id_str, src_mac)
            state_machine = MacAuthenticationBypassStateMachine(self.radius_output_messages,
                                                                src_mac,
                                                                self.timer_scheduler,
                                                                self.auth_success,
                                                                self.auth_failure,
                                                                log_prefix)
            self.state_machines[port_id_str][src_mac_str] = state_machine
            return state_machine

        if not state_machine:
            self.logger.debug("Creating EAP FULL State Machine")
            log_prefix = "%s.SM - port: %s, client: %s" % (self.logger.name, port_id_str, src_mac)
            state_machine = FullEAPStateMachine(self.eap_output_messages,
                                                self.radius_output_messages, src_mac,
                                                self.timer_scheduler, self.auth_success,
                                                self.auth_failure, self.auth_logoff,
                                                log_prefix)
            self.state_machines[port_id_str][src_mac_str] = state_machine
            self.logger.debug("created new state machine for '%s' on port '%s'",
                              src_mac_str, port_id_str)

        return state_machine
