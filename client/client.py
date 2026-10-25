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
APN = "airtelgprs.com"
PDP_IP_TYPE = 0                   # 0 = IPv4, 1 = IPv6, 2 = IPv4v6; server is IPv4-only
PROJECT_NAME = "UartTcpBridge"
PROJECT_VERSION = "1.0.0"
SIMULATE_METER = True           # True: answer the server from dummy_meter.py instead of the UART

# ---------------- globals -----------------
uart = UART(UART_PORT, UART_BAUD, 8, 0, 1, 0)
dummy = None
if SIMULATE_METER:
    try:
        from usr import dummy_meter   # QuecPython: files in /usr import as usr.<name>
    except ImportError:
        import dummy_meter
    dummy = dummy_meter.DummyMeter()
wdt = WDT(60)
sock = None
sock_ok = False                   # set False by either thread on error
lock = _thread.allocate_lock()


def print_data_call():
    """Show APN / IP / DNS so a dead data path is visible in the log."""
    try:
        import dataCall
        print("PDP info:", dataCall.getInfo(1, 0))   # (profile, ipType, [state, reconnect, ip, dns1, dns2])
    except Exception as e:
        print("PDP info error:", e)
    try:
        print("PDP context:", dataCall.getPDPContext(1))  # (ipType, apn, user, pwd, auth)
    except Exception as e:
        print("PDP context error:", e)


def ensure_pdp_context():
    """Make profile 1 use APN / PDP_IP_TYPE; reboots once if it had to change it."""
    import dataCall
    try:
        ctx = dataCall.getPDPContext(1)
    except Exception as e:
        print("PDP context read error:", e)
        return
    if ctx and ctx[0] == PDP_IP_TYPE and ctx[1] == APN:
        return
    print("PDP context", ctx, "-> setting", PDP_IP_TYPE, APN)
    if dataCall.setPDPContext(1, PDP_IP_TYPE, APN, "", "", 0) != 0:
        print("setPDPContext failed")
        return
    print("PDP context saved, rebooting to apply")
    utime.sleep(1)
    from misc import Power
    Power.powerRestart()


def wait_network():
    checknet = checkNet.CheckNetwork(PROJECT_NAME, PROJECT_VERSION)
    while True:
        wdt.feed()
        stage, state = checknet.wait_network_connected(20)
        if stage == 3 and state == 1:
            print("Network ready")
            print_data_call()
            return
        print("Waiting for network...", stage, state)


def is_ip(host):
    parts = host.split(".")
    return len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)


def resolve(host, port):
    if is_ip(host):                   # no DNS needed for a literal IP
        return (host, port)
    info = usocket.getaddrinfo(host, port)
    if not info:
        raise OSError("DNS lookup failed for " + host)
    return info[0][-1]


def tcp_connect():
    global sock, sock_ok
    step = "resolve"
    s = None
    try:
        addr = resolve(SERVER_HOST, SERVER_PORT)
        step = "socket"
        s = usocket.socket(usocket.AF_INET, usocket.SOCK_STREAM)
        step = "connect " + str(addr)
        s.settimeout(15)          # connect timeout only
        s.connect(addr)
        s.settimeout(None)        # blocking recv in the RX thread
    except Exception as e:
        if s:
            try:
                s.close()         # don't leak a socket on every retry
            except Exception:
                pass
        raise OSError("{} failed: {!r}".format(step, e))
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
            if dummy:
                for reply in dummy.feed(data):
                    s.send(reply)
                print("S->SIM", len(data), "bytes")
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
    ensure_pdp_context()
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