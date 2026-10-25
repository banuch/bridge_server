# -*- coding: UTF-8 -*-
"""EC200U QuecPython transparent DLMS/HDLC UART-to-TCP bridge."""

import ubinascii
import usocket
import utime
import checkNet
import dataCall
from machine import Pin, UART, WDT


# Vultr bridge_server/server/server.py endpoint.
SERVER_HOST = "65.20.77.137"
SERVER_PORT = 5000
DEVICE_ID = "EC200U_001"

# Meter serial settings from the reference bridge project.
UART_PORT = UART.UART2
UART_BAUD = 9600
UART_DATA_BITS = 8
UART_PARITY = 0       # 0=None, 1=Even, 2=Odd
UART_STOP_BITS = 1
UART_FLOW_CONTROL = 0

# Keep the board's GPIO4/DTR output HIGH. The standalone probe confirmed that
# the meter answers at both DTR levels, and HIGH is the required idle level.
DTR_OUTPUT_LEVEL = 1

FRAME_GAP_MS = 50
MAX_CHUNK = 1024
CONNECT_TIMEOUT_SECONDS = 15
SOCKET_WRITE_TIMEOUT_MS = 5000
NETWORK_WAIT_SECONDS = 30
RECONNECT_DELAY_SECONDS = 5
BOOT_DELAY_SECONDS = 3
TRACE_HEX = True

PROJECT_NAME = "EC200U_DLMS_BRIDGE"
PROJECT_VERSION = "1.0.0"


sock = None
sock_ok = False


def trace_data(direction, data):
    """Print relayed bytes without modifying them."""
    timestamp_ms = utime.ticks_ms()
    if TRACE_HEX:
        print("[%d ms] %s %d bytes: %s" % (
            timestamp_ms,
            direction,
            len(data),
            ubinascii.hexlify(data).upper(),
        ))
    else:
        print("[%d ms] %s %d bytes" % (
            timestamp_ms,
            direction,
            len(data),
        ))


def configure_dtr_gpio():
    """Configure GPIO4 as an output and keep it HIGH."""
    pin = Pin(Pin.GPIO4, Pin.OUT, Pin.PULL_DISABLE, DTR_OUTPUT_LEVEL)
    pin.write(DTR_OUTPUT_LEVEL)
    print("DTR GPIO4 configured HIGH, level:", pin.read())
    return pin


def configure_meter_uart():
    """Open the meter UART using the reference project's 9600 8N1 settings."""
    port = UART(
        UART_PORT,
        UART_BAUD,
        UART_DATA_BITS,
        UART_PARITY,
        UART_STOP_BITS,
        UART_FLOW_CONTROL,
    )
    print("Meter UART2 configured: 9600 8N1")
    return port


def create_watchdog():
    """Enable a watchdog when supported by this firmware."""
    try:
        watchdog = WDT(60)
        print("Watchdog enabled: 60 seconds")
        return watchdog
    except Exception as error:
        print("Watchdog unavailable:", error)
        return None


def feed_watchdog(watchdog):
    if watchdog is not None:
        watchdog.feed()


def wait_for_network(timeout_seconds):
    """Support both old and new QuecPython checkNet APIs."""
    if hasattr(checkNet, "waitNetworkReady"):
        return checkNet.waitNetworkReady(timeout_seconds)

    checker = checkNet.CheckNetwork(PROJECT_NAME, PROJECT_VERSION)
    return checker.wait_network_connected(timeout_seconds)


def wait_until_network_ready(watchdog):
    """Wait until PDP context 1 is usable and has an IPv4 address."""
    while True:
        feed_watchdog(watchdog)
        stage, state = wait_for_network(NETWORK_WAIT_SECONDS)
        print("Network check: stage=%s, state=%s" % (stage, state))

        if stage == 3 and state == 1:
            ipv4 = dataCall.getInfo(1, 0)
            ipv6 = dataCall.getInfo(1, 1)
            print("PDP IPv4:", ipv4)
            print("PDP IPv6:", ipv6)

            if ipv4 != -1 and len(ipv4) >= 3:
                ipv4_info = ipv4[2]
                if len(ipv4_info) >= 3 and ipv4_info[0] == 1:
                    return

            print("Network is registered, but profile 1 has no active IPv4 address")

        utime.sleep(RECONNECT_DELAY_SECONDS)


def send_all(active_socket, data):
    """Write every byte to a non-blocking socket with a bounded wait."""
    sent_total = 0
    data_length = len(data)
    started = utime.ticks_ms()

    while sent_total < data_length:
        try:
            sent = active_socket.write(data[sent_total:])
        except Exception as error:
            if is_would_block_error(error):
                if utime.ticks_diff(
                    utime.ticks_ms(), started
                ) >= SOCKET_WRITE_TIMEOUT_MS:
                    raise OSError("Socket write timed out")
                utime.sleep_ms(5)
                continue
            raise

        if sent is None:
            sent = data_length - sent_total
        if sent <= 0:
            raise OSError("Socket write returned %s" % sent)
        sent_total += sent

    return sent_total


def write_uart_all(port, data):
    """Write every byte to the meter UART or raise if it stalls."""
    written_total = 0
    data_length = len(data)

    while written_total < data_length:
        written = port.write(data[written_total:])
        if written is None:
            written = data_length - written_total
        if written <= 0:
            raise OSError("UART write returned %s" % written)
        written_total += written

    return written_total


def close_socket():
    """Mark the link down and release its socket."""
    global sock, sock_ok

    old_socket = sock
    sock = None
    sock_ok = False

    if old_socket is not None:
        try:
            old_socket.close()
        except Exception:
            pass


def socket_error_code(error):
    """Extract an integer errno from a QuecPython socket exception."""
    code = getattr(error, "errno", None)
    if isinstance(code, int):
        return code

    arguments = getattr(error, "args", ())
    if arguments and isinstance(arguments[0], int):
        return arguments[0]
    return None


def is_would_block_error(error):
    """Return True when a non-blocking socket simply has no data yet."""
    code = socket_error_code(error)
    if code == 11 or code == 41:
        return True

    message = str(error)
    return "EAGAIN" in message or "EWOULDBLOCK" in message


def receive_tcp_nonblocking(active_socket):
    """Read available TCP bytes without delaying UART processing."""
    try:
        data = active_socket.recv(MAX_CHUNK)
    except Exception as error:
        if is_would_block_error(error):
            return None
        raise

    if data is None:
        return None
    if not data:
        raise OSError("Server closed the TCP connection")
    return data


def connect_server():
    """Connect to Vultr and send the bridge identification line."""
    global sock, sock_ok

    address_info = usocket.getaddrinfo(
        SERVER_HOST,
        SERVER_PORT,
        usocket.AF_INET,
    )
    if not address_info:
        raise OSError("No IPv4 address returned for bridge server")

    server_address = address_info[0][-1]
    new_socket = usocket.socket(usocket.AF_INET, usocket.SOCK_STREAM)

    try:
        new_socket.settimeout(CONNECT_TIMEOUT_SECONDS)
        print("Connecting to:", server_address)
        new_socket.connect(server_address)
        new_socket.settimeout(None)

        hello = (
            '{"type":"BRIDGE","deviceId":"%s"}\n' % DEVICE_ID
        ).encode()
        send_all(new_socket, hello)
        new_socket.setblocking(False)

        sock = new_socket
        sock_ok = True

        print("TCP CONNECTED; bridge hello sent")

    except Exception:
        new_socket.close()
        raise


def read_uart_frame(meter_uart):
    """Collect UART bytes until a 50 ms silent gap or MAX_CHUNK is reached."""
    if meter_uart.any() == 0:
        return None

    frame = b""
    last_receive_ms = utime.ticks_ms()

    while utime.ticks_diff(utime.ticks_ms(), last_receive_ms) < FRAME_GAP_MS:
        available = meter_uart.any()
        if available:
            remaining = MAX_CHUNK - len(frame)
            if remaining <= 0:
                break

            data = meter_uart.read(min(available, remaining))
            if data:
                frame += data
                last_receive_ms = utime.ticks_ms()
        else:
            utime.sleep_ms(2)

    return frame if frame else None


def drain_uart(meter_uart):
    """Discard stale serial bytes before starting a new TCP bridge session."""
    discarded = 0
    while meter_uart.any():
        data = meter_uart.read(min(meter_uart.any(), MAX_CHUNK))
        if not data:
            break
        discarded += len(data)
    if discarded:
        print("Discarded %d stale UART bytes" % discarded)


def run():
    """Run the boot-ready transparent bridge forever."""
    global sock_ok

    print("=== EC200U DLMS/HDLC UART-TCP bridge ===")
    dtr_pin = configure_dtr_gpio()
    meter_uart = configure_meter_uart()
    watchdog = create_watchdog()

    utime.sleep(BOOT_DELAY_SECONDS)
    wait_until_network_ready(watchdog)

    while True:
        feed_watchdog(watchdog)

        if dtr_pin.read() != DTR_OUTPUT_LEVEL:
            dtr_pin.write(DTR_OUTPUT_LEVEL)
            print("DTR GPIO4 restored HIGH")

        try:
            if sock is None or not sock_ok:
                close_socket()
                wait_until_network_ready(watchdog)
                # Clear stale bytes before making the socket visible to the
                # TCP receive thread. Draining after connect can discard a
                # meter's fast reply to the server's first request.
                drain_uart(meter_uart)
                connect_server()

            # The socket and UART are both serviced by this single loop.
            # Non-blocking TCP receive ensures an immediate meter response is
            # read without waiting for more server traffic.
            active_socket = sock
            server_data = receive_tcp_nonblocking(active_socket)
            if server_data:
                trace_data("SERVER->METER", server_data)
                written = write_uart_all(meter_uart, server_data)
                print("[%d ms] UART TX complete: %d/%d bytes" % (
                    utime.ticks_ms(),
                    written,
                    len(server_data),
                ))

            frame = read_uart_frame(meter_uart)
            if frame:
                active_socket = sock
                if active_socket is None or not sock_ok:
                    raise OSError("TCP connection is unavailable")
                trace_data("METER->SERVER", frame)
                sent = send_all(active_socket, frame)
                print("[%d ms] TCP TX complete: %d/%d bytes" % (
                    utime.ticks_ms(),
                    sent,
                    len(frame),
                ))

            utime.sleep_ms(5)

        except KeyboardInterrupt:
            print("Bridge stopped by user")
            close_socket()
            return

        except Exception as error:
            print("Bridge error:", error)
            close_socket()
            print("Reconnect in %d seconds..." % RECONNECT_DELAY_SECONDS)
            utime.sleep(RECONNECT_DELAY_SECONDS)


run()
