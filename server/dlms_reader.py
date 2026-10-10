"""
dlms_reader.py - read basic details from a DLMS/COSEM meter through the bridge.

Gurux only builds request bytes and parses reply bytes; MeterSession moves
those bytes over the module's TCP connection (server.py feeds received data
in via feed()).

Session flow:  SNRM -> UA  |  AARQ -> AARE (LLS)  |  GET ...  |  RLRQ, DISC

Settings below mirror Gurux Director: Indian Standard, HDLC, LN referencing,
Authentication MR (= LOW), client 0x20, logical server 0, physical server 1.
"""
import asyncio
import binascii

from gurux_dlms import GXByteBuffer, GXDLMSClient, GXDLMSException, GXReplyData
from gurux_dlms.enums import Authentication, InterfaceType, Standard, Unit
from gurux_dlms.objects import GXDLMSClock, GXDLMSData, GXDLMSRegister

# ---------------- meter settings ----------------
CLIENT_ADDRESS = 0x20         # 32 = Meter Reader (MR) association
LOGICAL_SERVER = 0
PHYSICAL_SERVER = 1
AUTHENTICATION = Authentication.LOW
PASSWORD = "lnt1"
WAIT_TIME = 5                 # seconds to wait for each reply
RESEND_COUNT = 3              # resends of a frame before giving up
TRACE = True                  # log every TX/RX frame in hex

# (group, label, COSEM class, OBIS). Registers get scaler/unit applied.
READ_LIST = [
    ("Identity", "Serial number", GXDLMSData, "0.0.96.1.0.255"),
    ("Identity", "Manufacturer", GXDLMSData, "0.0.96.1.1.255"),
    ("Identity", "Meter type", GXDLMSData, "0.0.94.91.9.255"),
    ("Identity", "Firmware version", GXDLMSData, "1.0.0.2.0.255"),
    ("Clock", "Date/time", GXDLMSClock, "0.0.1.0.0.255"),
    ("Instantaneous", "Voltage (1-ph)", GXDLMSRegister, "1.0.12.7.0.255"),
    ("Instantaneous", "Voltage L1", GXDLMSRegister, "1.0.32.7.0.255"),
    ("Instantaneous", "Voltage L2", GXDLMSRegister, "1.0.52.7.0.255"),
    ("Instantaneous", "Voltage L3", GXDLMSRegister, "1.0.72.7.0.255"),
    ("Instantaneous", "Current (1-ph)", GXDLMSRegister, "1.0.11.7.0.255"),
    ("Instantaneous", "Current L1", GXDLMSRegister, "1.0.31.7.0.255"),
    ("Instantaneous", "Current L2", GXDLMSRegister, "1.0.51.7.0.255"),
    ("Instantaneous", "Current L3", GXDLMSRegister, "1.0.71.7.0.255"),
    ("Instantaneous", "Power factor", GXDLMSRegister, "1.0.13.7.0.255"),
    ("Instantaneous", "Frequency", GXDLMSRegister, "1.0.14.7.0.255"),
    ("Instantaneous", "Active power", GXDLMSRegister, "1.0.1.7.0.255"),
    ("Energy", "Import active energy", GXDLMSRegister, "1.0.1.8.0.255"),
    ("Energy", "Import apparent energy", GXDLMSRegister, "1.0.9.8.0.255"),
]

UNIT_TEXT = {
    Unit.VOLTAGE: "V", Unit.CURRENT: "A", Unit.FREQUENCY: "Hz",
    Unit.ACTIVE_POWER: "W", Unit.APPARENT_POWER: "VA", Unit.REACTIVE_POWER: "var",
    Unit.ACTIVE_ENERGY: "Wh", Unit.APPARENT_ENERGY: "VAh", Unit.REACTIVE_ENERGY: "varh",
}


class LinkClosed(Exception):
    """The module disconnected while a session was running."""


def _hex(data):
    return binascii.hexlify(bytes(data), " ").decode().upper()


def _format(value):
    if isinstance(value, (bytes, bytearray)):
        if value and all(32 <= b < 127 for b in value):
            return value.decode("ascii")
        return _hex(value)
    if isinstance(value, float):
        return "{:g}".format(round(value, 3))
    return str(value)


class MeterSession:
    def __init__(self, cid, writer, log):
        self.cid = cid
        self.writer = writer
        self.log = log
        self.rx = asyncio.Queue()
        self.resends = RESEND_COUNT
        server_address = GXDLMSClient.getServerAddress(LOGICAL_SERVER, PHYSICAL_SERVER)
        self.client = GXDLMSClient(True, CLIENT_ADDRESS, server_address,
                                   AUTHENTICATION, PASSWORD, InterfaceType.HDLC)
        self.client.standard = Standard.INDIA

    # ---- byte transport ----
    def feed(self, data):
        """Bytes received from the module (None = connection closed)."""
        self.rx.put_nowait(data)

    async def _exchange(self, frame, reply):
        """Send one frame and wait until Gurux has a complete reply."""
        for attempt in range(1 + self.resends):
            while not self.rx.empty():          # drop stale bytes
                if self.rx.get_nowait() is None:
                    raise LinkClosed()
            if TRACE:
                self.log("DLMS TX #{}: {}".format(self.cid, _hex(frame)))
            self.writer.write(bytes(frame))
            await self.writer.drain()
            buf = GXByteBuffer()
            notify = GXReplyData()
            try:
                while True:
                    chunk = await asyncio.wait_for(self.rx.get(), WAIT_TIME)
                    if chunk is None:
                        raise LinkClosed()
                    if TRACE:
                        self.log("DLMS RX #{}: {}".format(self.cid, _hex(chunk)))
                    buf.set(chunk)
                    if self.client.getData(buf, reply, notify):
                        break
            except asyncio.TimeoutError:
                self.log("DLMS #{}: no reply in {}s (attempt {}/{})".format(
                    self.cid, WAIT_TIME, attempt + 1, 1 + self.resends))
                continue
            if reply.error:
                raise GXDLMSException(reply.error)
            return
        raise TimeoutError("meter did not answer")

    async def _request(self, frames, reply):
        """Send a request (one frame or a list) and collect multi-block replies."""
        if not frames:
            return
        if not isinstance(frames, list):
            frames = [frames]
        for frame in frames:
            reply.clear()
            await self._exchange(frame, reply)
            while reply.isMoreData():
                await self._exchange(self.client.receiverReady(reply), reply)

    # ---- DLMS steps ----
    async def connect(self):
        reply = GXReplyData()
        await self._request(self.client.snrmRequest(), reply)
        self.client.parseUAResponse(reply.data)
        reply = GXReplyData()
        await self._request(self.client.aarqRequest(), reply)
        self.client.parseAareResponse(reply.data)
        if self.client.getIsAuthenticationRequired():      # HLS only
            reply = GXReplyData()
            await self._request(self.client.getApplicationAssociationRequest(), reply)
            self.client.parseApplicationAssociationResponse(reply.data)

    async def read_attribute(self, obj, index):
        reply = GXReplyData()
        await self._request(self.client.read(obj, index), reply)
        return self.client.updateValue(obj, index, reply.value)

    async def read_item(self, cls, obis):
        obj = cls(obis)
        if isinstance(obj, GXDLMSRegister):
            await self.read_attribute(obj, 3)           # scaler + unit first
            value = await self.read_attribute(obj, 2)   # then scaled value
            if obj.unit in (None, Unit.NONE, Unit.NO_UNIT):
                unit = ""
            else:
                unit = UNIT_TEXT.get(obj.unit, getattr(obj.unit, "name", str(obj.unit)))
            return "{} {}".format(_format(value), unit).strip()
        return _format(await self.read_attribute(obj, 2))

    async def close(self):
        """Release the association and drop the HDLC link; errors are ignored."""
        self.resends = 0
        # Build each request only when sending it: disconnectRequest() resets
        # the HDLC frame counters, which would break the RLRE check.
        for make in (self.client.releaseRequest, self.client.disconnectRequest):
            try:
                await self._request(make(), GXReplyData())
            except Exception:
                pass


async def read_basic(session):
    """Connect, read READ_LIST, disconnect. Returns [(group, label, obis, text)]."""
    results = []
    try:
        await session.connect()
        session.log("DLMS #{}: associated (client {}, server {}/{})".format(
            session.cid, CLIENT_ADDRESS, LOGICAL_SERVER, PHYSICAL_SERVER))
        for group, label, cls, obis in READ_LIST:
            try:
                text = await session.read_item(cls, obis)
            except GXDLMSException as e:
                text = "n/a ({})".format(e)
            results.append((group, label, obis, text))
    finally:
        await session.close()
    return results


def format_results(cid, results):
    lines = ["Meter details via module #{}".format(cid)]
    group = None
    for g, label, obis, text in results:
        if g != group:
            lines.append("  [{}]".format(g))
            group = g
        lines.append("    {:<24} {:<16} {}".format(label, obis, text))
    return "\n".join(lines)
