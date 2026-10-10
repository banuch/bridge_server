"""
bridge_server.py - TCP server for the QuecPython UART<->TCP bridge.

Run on your PC / VM:   python server.py
Needs Python 3.8+ and gurux-dlms (pip install -r requirements.txt).

What it does
 - Listens for modules connecting from the field.
 - Logs every chunk received from a meter (hex + ASCII) to the console and bridge_log.txt.
 - Lets you type commands to send bytes to a meter through its module:

       list                       show connected modules
       send <id> <hex bytes>      e.g.  send 1 7EA00A00020023219349E27E
       sendtext <id> <text>       send plain text (\\r \\n escapes allowed)
       read <id>                  DLMS: connect and read basic meter details
       kick <id>                  disconnect a module
       quit                       stop the server

Meter addresses / password live in dlms_reader.py.
"""
import asyncio
import binascii
import sys
import time

import dlms_reader

HOST = "0.0.0.0"
PORT = 5000                 # must match SERVER_PORT in the module script
IDLE_TIMEOUT = 600          # seconds without data before dropping a module
LOG_FILE = "bridge_log.txt"

clients = {}                # id -> (reader, writer, addr)
sessions = {}               # id -> MeterSession while a DLMS read is running
tasks = set()               # running read tasks (keeps references alive)
next_id = 1


def log(line):
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    text = "[{}] {}".format(stamp, line)
    print(text)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(text + "\n")


def handle_data(cid, addr, data):
    """Called for every chunk a meter sends. Put DLMS parsing here."""
    hexs = binascii.hexlify(data, " ").decode().upper()
    ascii_ = "".join(chr(b) if 32 <= b < 127 else "." for b in data)
    log("RX #{} {} ({} bytes)\n    HEX  : {}\n    ASCII: {}".format(
        cid, addr, len(data), hexs, ascii_))


async def handle_client(reader, writer):
    global next_id
    cid = next_id
    next_id += 1
    addr = writer.get_extra_info("peername")
    clients[cid] = (reader, writer, addr)
    log("Module #{} connected from {}".format(cid, addr))
    try:
        while True:
            data = await asyncio.wait_for(reader.read(2048), IDLE_TIMEOUT)
            if not data:
                break
            session = sessions.get(cid)
            if session:
                session.feed(data)
            else:
                handle_data(cid, addr, data)
    except asyncio.TimeoutError:
        log("Module #{} idle for {}s, dropping".format(cid, IDLE_TIMEOUT))
    except (ConnectionResetError, BrokenPipeError):
        pass
    finally:
        session = sessions.get(cid)
        if session:
            session.feed(None)          # abort a running read
        clients.pop(cid, None)
        writer.close()
        log("Module #{} disconnected".format(cid))


async def send_to(cid, payload):
    entry = clients.get(cid)
    if not entry:
        print("No such module:", cid)
        return
    _, writer, addr = entry
    writer.write(payload)
    await writer.drain()
    log("TX #{} {} ({} bytes): {}".format(
        cid, addr, len(payload), binascii.hexlify(payload, " ").decode().upper()))


async def read_meter(cid):
    entry = clients.get(cid)
    if not entry:
        print("No such module:", cid)
        return
    if cid in sessions:
        print("A read is already running on module", cid)
        return
    session = dlms_reader.MeterSession(cid, entry[1], log)
    sessions[cid] = session
    log("DLMS #{}: reading meter...".format(cid))
    try:
        results = await dlms_reader.read_basic(session)
        log(dlms_reader.format_results(cid, results))
    except dlms_reader.LinkClosed:
        log("DLMS #{}: module disconnected during read".format(cid))
    except Exception as e:
        log("DLMS #{}: read failed: {!r}".format(cid, e))
    finally:
        sessions.pop(cid, None)


async def console():
    loop = asyncio.get_running_loop()
    print("Commands: list | send <id> <hex> | sendtext <id> <text> | read <id> | kick <id> | quit")
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            await asyncio.sleep(1)
            continue
        parts = line.strip().split(None, 2)
        if not parts:
            continue
        cmd = parts[0].lower()
        try:
            if cmd == "list":
                if not clients:
                    print("(no modules connected)")
                for cid, (_, _, addr) in clients.items():
                    print("  #{}  {}".format(cid, addr))
            elif cmd == "send" and len(parts) == 3:
                payload = binascii.unhexlify(parts[2].replace(" ", ""))
                await send_to(int(parts[1]), payload)
            elif cmd == "sendtext" and len(parts) == 3:
                text = parts[2].encode().decode("unicode_escape").encode()
                await send_to(int(parts[1]), text)
            elif cmd == "read" and len(parts) >= 2:
                task = asyncio.create_task(read_meter(int(parts[1])))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
            elif cmd == "kick" and len(parts) >= 2:
                entry = clients.get(int(parts[1]))
                if entry:
                    entry[1].close()
            elif cmd == "quit":
                for _, w, _ in list(clients.values()):
                    w.close()
                asyncio.get_running_loop().stop()
                return
            else:
                print("Unknown or incomplete command")
        except (ValueError, binascii.Error) as e:
            print("Bad input:", e)


async def main():
    server = await asyncio.start_server(handle_client, HOST, PORT)
    log("Server listening on {}:{}".format(HOST, PORT))
    async with server:
        await asyncio.gather(server.serve_forever(), console())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, RuntimeError):
        print("\nStopped.")
