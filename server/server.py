"""
bridge_server.py - TCP server for the field bridges (QuecPython module or
ESP32 serial_tcp_bridge in cellular mode).

Run on your PC / VM:   python server.py
Needs Python 3.8+ and gurux-dlms (pip install -r requirements.txt).

What it does
 - Listens for modules connecting from the field on PORT.
 - An ESP32 bridge first sends one hello line
       {"type":"BRIDGE","deviceId":"ESP-xxxxxx"}
   which is used as the module's name and not passed on.
 - Logs every chunk received from a meter (hex + ASCII) to the console and bridge_log.txt.
 - Automatically reads a meter after its bridge hello. After a successful
   read and DLMS disconnect, the server closes TCP so the field module can
   wait for its next scheduled cycle.
 - PC relay on PC_PORT: a PC tool (Gurux Director: Media Net, TCP, host =
   this server, port = PC_PORT, interface HDLC) is piped byte-for-byte to one
   module. The target is the module chosen with "pipe <id>", else the module
   that connected last. One PC per module; no DLMS read while a PC is piped.
 - Lets you type commands to send bytes to a meter through its module:

       list                       show connected modules
       send <id> <hex bytes>      e.g.  send 1 7EA00A00020023219349E27E
       sendtext <id> <text>       send plain text (\\r \\n escapes allowed)
       read <id>                  DLMS: connect and read basic meter details
       pipe <id>                  route the next PC connection to module <id>
       pipe off                   back to "module that connected last"
       kick <id>                  disconnect a module
       quit                       stop the server

Meter addresses / password live in dlms_reader.py.
"""
import asyncio
import binascii
import json
import sys
import time

import dlms_reader

HOST = "0.0.0.0"
PORT = 5000                 # must match SERVER_PORT in the module script
PC_PORT = 4059              # PC tools connect here to reach a module
PC_ALLOW = []               # PC IPs allowed on PC_PORT, e.g. ["1.2.3.4"]; [] = any
IDLE_TIMEOUT = 600          # seconds without data before dropping a module / PC
RELAY_TRACE = True          # log relayed bytes in hex
LOG_FILE = "bridge_log.txt"
AUTO_READ_ON_CONNECT = True
AUTO_READ_DELAY = 1         # seconds after bridge hello before starting DLMS
AUTO_READ_RETRY = 60        # retry interval after an unsuccessful meter read

HELLO_PREFIX = b'{"type":"BRIDGE"'

clients = {}                # id -> (reader, writer, addr)
names = {}                  # id -> deviceId from the bridge hello
sessions = {}               # id -> MeterSession while a DLMS read is running
pc_links = {}               # module id -> writer of the PC piped to it
pipe_target = None          # module id for the next PC, None = last connected
tasks = set()               # running read tasks (keeps references alive)
auto_tasks = {}             # module id -> automatic read-cycle task
next_id = 1


def log(line):
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    text = "[{}] {}".format(stamp, line)
    print(text)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(text + "\n")


def _hex(data):
    return binascii.hexlify(data, " ").decode().upper()


def label(cid):
    name = names.get(cid)
    return "#{} ({})".format(cid, name) if name else "#{}".format(cid)


def take_hello(cid, data):
    """Strip a valid bridge hello; return (remaining_data, is_bridge)."""
    if not data.startswith(HELLO_PREFIX) or b"\n" not in data:
        return data, False
    line, rest = data.split(b"\n", 1)
    try:
        hello = json.loads(line.decode())
        if hello.get("type") != "BRIDGE":
            return data, False
        names[cid] = str(hello.get("deviceId", ""))
        log("Module #{} is bridge {}".format(cid, names[cid]))
    except (ValueError, UnicodeDecodeError, AttributeError):
        return data, False
    return rest, True


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
    first = True
    try:
        while True:
            data = await asyncio.wait_for(reader.read(2048), IDLE_TIMEOUT)
            if not data:
                break
            if first:
                first = False
                data, is_bridge = take_hello(cid, data)
                if is_bridge and AUTO_READ_ON_CONNECT:
                    task = asyncio.create_task(auto_read_cycle(cid))
                    auto_tasks[cid] = task
                    tasks.add(task)
                    task.add_done_callback(tasks.discard)
                if not data:
                    continue
            pc = pc_links.get(cid)
            session = sessions.get(cid)
            if pc:
                if RELAY_TRACE:
                    log("METER->PC {} ({} bytes): {}".format(label(cid), len(data), _hex(data)))
                pc.write(data)
                try:
                    await pc.drain()
                except (ConnectionResetError, BrokenPipeError):
                    pass
            elif session:
                session.feed(data)
            else:
                handle_data(cid, addr, data)
    except asyncio.TimeoutError:
        log("Module {} idle for {}s, dropping".format(label(cid), IDLE_TIMEOUT))
    except (ConnectionResetError, BrokenPipeError):
        pass
    finally:
        task = auto_tasks.pop(cid, None)
        if task and not task.done():
            task.cancel()
        session = sessions.get(cid)
        if session:
            session.feed(None)          # abort a running read
        pc = pc_links.pop(cid, None)
        if pc:
            pc.close()                  # the PC sees the meter link drop
        clients.pop(cid, None)
        writer.close()
        log("Module {} disconnected".format(label(cid)))
        names.pop(cid, None)


def pick_module():
    """Module for a new PC connection, or None."""
    if pipe_target is not None:
        return pipe_target if pipe_target in clients else None
    return max(clients) if clients else None


async def handle_pc(reader, writer):
    addr = writer.get_extra_info("peername")
    if PC_ALLOW and addr[0] not in PC_ALLOW:
        log("PC {} refused: not in PC_ALLOW".format(addr))
        writer.close()
        return
    cid = pick_module()
    reason = None
    if cid is None:
        reason = "no module connected" if pipe_target is None else \
            "module #{} not connected".format(pipe_target)
    elif cid in pc_links:
        reason = "module {} already has a PC".format(label(cid))
    elif cid in sessions:
        reason = "a DLMS read is running on module {}".format(label(cid))
    if reason:
        log("PC {} refused: {}".format(addr, reason))
        writer.close()
        return
    pc_links[cid] = writer
    module_writer = clients[cid][1]
    log("PC {} piped to module {}".format(addr, label(cid)))
    try:
        while True:
            data = await asyncio.wait_for(reader.read(2048), IDLE_TIMEOUT)
            if not data or cid not in clients:
                break
            if RELAY_TRACE:
                log("PC->METER {} ({} bytes): {}".format(label(cid), len(data), _hex(data)))
            module_writer.write(data)
            await module_writer.drain()
    except asyncio.TimeoutError:
        log("PC {} idle for {}s, dropping".format(addr, IDLE_TIMEOUT))
    except (ConnectionResetError, BrokenPipeError):
        pass
    finally:
        if pc_links.get(cid) is writer:
            del pc_links[cid]
        writer.close()
        log("PC {} disconnected".format(addr))


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
        return False
    if cid in sessions:
        print("A read is already running on module", cid)
        return False
    if cid in pc_links:
        print("A PC is piped to module", cid)
        return False
    session = dlms_reader.MeterSession(cid, entry[1], log)
    sessions[cid] = session
    log("DLMS #{}: reading meter...".format(cid))
    success = False
    try:
        results = await dlms_reader.read_basic(session)
        log(dlms_reader.format_results(cid, results))
        success = True
    except dlms_reader.LinkClosed:
        log("DLMS #{}: module disconnected during read".format(cid))
    except Exception as e:
        log("DLMS #{}: read failed: {!r}".format(cid, e))
    finally:
        sessions.pop(cid, None)
    return success


async def auto_read_cycle(cid):
    """Read until successful, then close TCP to start the module's timer."""
    try:
        await asyncio.sleep(AUTO_READ_DELAY)

        while cid in clients:
            success = await read_meter(cid)
            if success:
                entry = clients.get(cid)
                if entry:
                    log("Automatic read {} complete; closing TCP for next cycle".format(
                        label(cid)))
                    entry[1].close()
                return

            if cid in clients:
                log("Automatic read {} unsuccessful; retrying in {}s".format(
                    label(cid), AUTO_READ_RETRY))
                await asyncio.sleep(AUTO_READ_RETRY)
    except asyncio.CancelledError:
        pass
    finally:
        current = asyncio.current_task()
        if auto_tasks.get(cid) is current:
            auto_tasks.pop(cid, None)


async def console():
    global pipe_target
    loop = asyncio.get_running_loop()
    print("Commands: list | send <id> <hex> | sendtext <id> <text> | read <id> | "
          "pipe <id>|off | kick <id> | quit")
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
                    pc = pc_links.get(cid)
                    print("  {}  {}{}".format(label(cid), addr, "  <- PC {}".format(
                        pc.get_extra_info("peername")) if pc else ""))
                print("PC target: {}".format(
                    "module #{}".format(pipe_target) if pipe_target is not None else "last connected"))
            elif cmd == "pipe" and len(parts) >= 2:
                pipe_target = None if parts[1].lower() == "off" else int(parts[1])
                print("Next PC goes to", "the last connected module" if pipe_target is None
                      else "module #{}".format(pipe_target))
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
    pc_server = await asyncio.start_server(handle_pc, HOST, PC_PORT)
    log("Server listening on {}:{} (modules), {}:{} (PC relay)".format(HOST, PORT, HOST, PC_PORT))
    async with server, pc_server:
        await asyncio.gather(server.serve_forever(), pc_server.serve_forever(), console())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, RuntimeError):
        print("\nStopped.")
