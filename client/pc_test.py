"""
pc_test.py - pretend to be a module with a meter attached, from a PC.

    python pc_test.py [host] [port]        (default 127.0.0.1 5000)

Connects to server.py like the Quectel module would and answers with
dummy_meter.py. Then type  read 1  on the server console.
"""
import socket
import sys

from dummy_meter import DummyMeter

host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
port = int(sys.argv[2]) if len(sys.argv) > 2 else 5000

meter = DummyMeter()
sock = socket.create_connection((host, port))
print("Connected to {}:{} as a simulated module (Ctrl+C to stop)".format(host, port))
try:
    while True:
        data = sock.recv(2048)
        if not data:
            print("Server closed connection")
            break
        for reply in meter.feed(data):
            sock.sendall(reply)
except KeyboardInterrupt:
    pass
finally:
    sock.close()
