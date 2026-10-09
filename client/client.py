"""
uart_tcp_bridge.py - transparent UART <-> TCP bridge for QuecPython.

Meter (UART) <-> module <-> TCP server (your DLMS gateway)

 - Bytes from the meter are collected until the line goes quiet
   (FRAME_GAP_MS), then sent to the server as one chunk.
 - Bytes from the server are written straight to the meter.
 - Reconnects automatically and feeds a watchdog.

Save as /usr/main.py to auto-run at power-up.
"""
import utime
import _thread
import usocket
import checkNet
from machine import UART, WDT

# ---------------- settings ----------------
SERVER_HOST = "65.20.77.137"   # change to your gateway IP / domain
SERVER_PORT = 5000                # change to your gateway port
UART_PORT = UART.UART2
UART_BAUD = 9600                  # match your meter (DLMS optical is often 300 7E1 -> 9600 8N1 after negotiation)
FRAME_GAP_MS = 50                 # silence that marks the end of a frame
MAX_CHUNK = 1024
PROJECT_NAME = "UartTcpBridge"
PROJECT_VERSION = "1.0.0"

# ---------------- globals -----------------
uart = UART(UART_PORT, UART_BAUD, 8, 0, 1, 0)
wdt = WDT(60)
sock = None
sock_ok = False                   # set False by either thread on error
lock = _thread.allocate_lock()


def wait_network():
    checknet = checkNet.CheckNetwork(PROJECT_NAME, PROJECT_VERSION)
    while True:
        wdt.feed()
        stage, state = checknet.wait_network_connected(20)
        if stage == 3 and state == 1:
            print("Network ready")
            return
        print("Waiting for network...", stage, state)


def tcp_connect():
    global sock, sock_ok
    addr = usocket.getaddrinfo(SERVER_HOST, SERVER_PORT)[0][-1]
    s = usocket.socket(usocket.AF_INET, usocket.SOCK_STREAM)
    s.settimeout(15)              # connect timeout only
    s.connect(addr)
    s.settimeout(None)            # blocking recv in the RX thread
    with lock:
        sock = s
        sock_ok = True
    print("TCP connected to", SERVER_HOST, SERVER_PORT)


def tcp_close():
    global sock, sock_ok
    with lock:
        sock_ok = False
        if sock:
            try:
                sock.close()
            except Exception:
                pass
            sock = None


def tcp_to_uart_thread():
    """Server -> meter. Runs forever; exits its loop body on error."""
    global sock_ok
    while True:
        s = sock
        if s is None or not sock_ok:
            utime.sleep_ms(200)
            continue
        try:
            data = s.recv(MAX_CHUNK)
            if not data:                      # server closed
                print("Server closed connection")
                sock_ok = False
                continue
            uart.write(data)
            print("S->M", len(data), "bytes")
        except Exception as e:
            print("RX error:", e)
            sock_ok = False
            utime.sleep_ms(200)


def uart_to_tcp_once():
    """Meter -> server. Collect one frame, then send it."""
    global sock_ok
    if uart.any() == 0:
        return
    buf = b""
    last_rx = utime.ticks_ms()
    while utime.ticks_diff(utime.ticks_ms(), last_rx) < FRAME_GAP_MS:
        n = uart.any()
        if n:
            buf += uart.read(n)
            last_rx = utime.ticks_ms()
            if len(buf) >= MAX_CHUNK:
                break
        else:
            utime.sleep_ms(5)
    if buf:
        sock.send(buf)
        print("M->S", len(buf), "bytes")


def main():
    global sock_ok
    utime.sleep(5)                            # boot delay
    wait_network()
    _thread.start_new_thread(tcp_to_uart_thread, ())

    while True:
        wdt.feed()
        try:
            if sock is None or not sock_ok:
                tcp_close()
                tcp_connect()
            uart_to_tcp_once()
            utime.sleep_ms(10)
        except Exception as e:
            print("Bridge error:", e)
            tcp_close()
            utime.sleep(5)
            wait_network()


main()