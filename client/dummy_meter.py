"""
dummy_meter.py - minimal simulated DLMS/COSEM meter (HDLC, LN, LLS).

Lets the bridge be tested without a physical meter. Runs on QuecPython and on
a PC (see pc_test.py). Copy to /usr/ next to main.py on the module.

Understands only what the server's dlms_reader sends:
    SNRM -> UA, AARQ -> AARE (checks password), GET -> GET response,
    RLRQ -> RLRE, DISC -> UA
"""
try:
    import utime as time
except ImportError:
    import time

CLIENT_ADDRESS = 0x20         # meter only answers this client
SERVER_ADDRESS = 1            # logical 0, physical 1
PASSWORD = b"lnt1"

# DLMS data-access-result codes
OBJECT_UNDEFINED = 4
READ_WRITE_DENIED = 3

# ---------------- A-XDR encoders ----------------


def octets(s):
    if isinstance(s, str):
        s = s.encode()
    return bytes([0x09, len(s)]) + s


def u8(v):
    return bytes([0x11, v & 0xFF])


def u16(v):
    return bytes([0x12, (v >> 8) & 0xFF, v & 0xFF])


def i16(v):
    v &= 0xFFFF
    return bytes([0x10, v >> 8, v & 0xFF])


def u32(v):
    return bytes([0x06, (v >> 24) & 0xFF, (v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF])


def scaler_unit(scaler, unit):
    return bytes([0x02, 0x02, 0x0F, scaler & 0xFF, 0x16, unit])


def clock_now():
    t = time.localtime()       # (year, month, day, hour, min, sec, weekday 0=Mon, ...)
    year = t[0]
    return octets(bytes([
        year >> 8, year & 0xFF, t[1], t[2], t[6] + 1,
        t[3], t[4], t[5], 0xFF,     # hundredths not specified
        0x80, 0x00,                 # deviation not specified
        0x00,                       # clock status
    ]))


def obis(text):
    return bytes([int(x) for x in text.split(".")])


# (class id, OBIS) -> {attribute: encoded value or function returning one}
V, A, HZ, W, WH, VAH, NO_UNIT = 35, 33, 44, 27, 30, 31, 255
OBJECTS = {
    (1, obis("0.0.96.1.0.255")): {2: octets("LT00012345")},        # serial number
    (1, obis("0.0.96.1.1.255")): {2: octets("L&T SIM")},           # manufacturer
    (1, obis("0.0.94.91.9.255")): {2: u8(5)},                      # meter type
    (1, obis("1.0.0.2.0.255")): {2: octets("SIM-1.0")},            # firmware
    (8, obis("0.0.1.0.0.255")): {2: clock_now},                    # clock
    (3, obis("1.0.12.7.0.255")): {2: u16(2314), 3: scaler_unit(-1, V)},       # 231.4 V
    (3, obis("1.0.11.7.0.255")): {2: u32(5120), 3: scaler_unit(-3, A)},       # 5.12 A
    (3, obis("1.0.13.7.0.255")): {2: i16(985), 3: scaler_unit(-3, NO_UNIT)},  # PF 0.985
    (3, obis("1.0.14.7.0.255")): {2: u16(5002), 3: scaler_unit(-2, HZ)},      # 50.02 Hz
    (3, obis("1.0.1.7.0.255")): {2: u32(1162), 3: scaler_unit(0, W)},         # 1162 W
    (3, obis("1.0.1.8.0.255")): {2: u32(1234567), 3: scaler_unit(0, WH)},     # kWh
    (3, obis("1.0.9.8.0.255")): {2: u32(1302040), 3: scaler_unit(0, VAH)},    # kVAh
}

# ---------------- HDLC ----------------


def _fcs16(data):
    fcs = 0xFFFF
    for b in data:
        fcs ^= b
        for _ in range(8):
            fcs = (fcs >> 1) ^ 0x8408 if fcs & 1 else fcs >> 1
    fcs ^= 0xFFFF
    return bytes([fcs & 0xFF, fcs >> 8])


def _hdlc_addr(value):
    """Encode an HDLC address (1, 2 or 4 bytes, LSB of last byte = 1)."""
    if value < 0x80:
        return bytes([(value << 1) | 1])
    if value < 0x4000:
        return bytes([(value >> 6) & 0xFE, ((value << 1) & 0xFE) | 1])
    raise ValueError("address too large")


def _frame(dest, src, control, info=b""):
    length = 2 + len(dest) + len(src) + 1 + 2 + (len(info) + 2 if info else 0)
    head = bytes([0xA0 | ((length >> 8) & 0x07), length & 0xFF]) + dest + src + bytes([control])
    body = head + _fcs16(head)
    if info:
        body += info + _fcs16(body + info)
    return b"\x7e" + body + b"\x7e"


class DummyMeter:
    def __init__(self, log=print):
        self.log = log
        self.buf = b""
        self.ns = 0              # our send sequence
        self.nr = 0              # next sequence we expect from the client
        self.connected = False   # HDLC link up
        self.associated = False  # AARQ accepted

    def feed(self, data):
        """Bytes from the server. Returns a list of reply frames (bytes)."""
        self.buf += bytes(data)
        replies = []
        while True:
            start = self.buf.find(b"\x7e")
            if start < 0:
                self.buf = b""
                break
            self.buf = self.buf[start:]
            if len(self.buf) < 3:
                break
            if self.buf[1] & 0xF0 != 0xA0:       # flag between frames; skip it
                self.buf = self.buf[1:]
                continue
            total = (((self.buf[1] & 0x07) << 8) | self.buf[2]) + 2
            if len(self.buf) < total:
                break
            frame, self.buf = self.buf[:total], self.buf[total:]
            reply = self._handle_frame(frame)
            if reply:
                replies.append(reply)
        return replies

    def _handle_frame(self, frame):
        body = frame[1:-1]
        if frame[-1] != 0x7E or _fcs16(body[:-2]) != body[-2:]:
            self.log("SIM: bad frame, ignored")
            return None
        pos = 2
        dest_start = pos
        while not body[pos] & 1:
            pos += 1
        pos += 1
        dest = body[dest_start:pos]
        src = body[pos:pos + 1]
        pos += 1
        if dest != _hdlc_addr(SERVER_ADDRESS) or src != _hdlc_addr(CLIENT_ADDRESS):
            self.log("SIM: frame for other address, ignored")
            return None
        control = body[pos]
        info = body[pos + 3:-2] if len(body) > pos + 3 else b""

        if control & 0xEF == 0x83:               # SNRM
            self.ns = self.nr = 0
            self.connected = True
            self.associated = False
            self.log("SIM: SNRM -> UA")
            ua = bytes([0x81, 0x80, 0x12, 0x05, 0x01, 0x80, 0x06, 0x01, 0x80,
                        0x07, 0x04, 0, 0, 0, 1, 0x08, 0x04, 0, 0, 0, 1])
            return _frame(src, dest, 0x73, ua)
        if control & 0xEF == 0x43:               # DISC
            was = self.connected
            self.connected = self.associated = False
            self.log("SIM: DISC -> " + ("UA" if was else "DM"))
            return _frame(src, dest, 0x73 if was else 0x1F)
        if not self.connected:
            return _frame(src, dest, 0x1F)       # DM: not connected
        if control & 0x01 == 0:                  # I-frame
            self.nr = (((control >> 1) & 0x07) + 1) % 8
            apdu = info[3:] if info[:3] == b"\xe6\xe6\x00" else info
            out = self._handle_apdu(apdu)
            ctrl = (self.nr << 5) | 0x10 | (self.ns << 1)
            self.ns = (self.ns + 1) % 8
            return _frame(src, dest, ctrl, b"\xe6\xe7\x00" + out)
        if control & 0x0F == 0x01:               # RR
            return _frame(src, dest, (self.nr << 5) | 0x11)
        return None

    # ---------------- APDUs ----------------

    def _handle_apdu(self, apdu):
        tag = apdu[0]
        if tag == 0x60:
            return self._aare(apdu)
        if tag == 0x62:                          # RLRQ
            self.associated = False
            self.log("SIM: RLRQ -> RLRE")
            return bytes([0x63, 0x03, 0x80, 0x01, 0x00])
        if tag == 0xC0 and apdu[1] == 0x01:      # GET-Request-Normal
            return self._get(apdu)
        self.log("SIM: unsupported APDU {:02X}".format(tag))
        return bytes([0xD8, 0x01, 0x01])         # exception response

    def _aare(self, aarq):
        password = None
        pos = 2
        while pos < len(aarq):
            tag, ln = aarq[pos], aarq[pos + 1]
            if tag == 0xAC and aarq[pos + 2] == 0x80:     # calling-authentication-value
                password = bytes(aarq[pos + 4:pos + 4 + aarq[pos + 3]])
            pos += 2 + ln
        ok = password == PASSWORD
        self.associated = ok
        self.log("SIM: AARQ -> AARE ({})".format("accepted" if ok else "wrong password"))
        result = 0x00 if ok else 0x01                     # accepted / rejected-permanent
        diag = bytes([0xA1, 0x03, 0x02, 0x01, 0x00]) if ok else bytes([0xA1, 0x03, 0x02, 0x01, 0x0D])
        body = (bytes([0xA1, 0x09, 0x06, 0x07, 0x60, 0x85, 0x74, 0x05, 0x08, 0x01, 0x01])
                + bytes([0xA2, 0x03, 0x02, 0x01, result])
                + bytes([0xA3, len(diag)]) + diag)
        if ok:
            init = bytes([0x08, 0x00, 0x06, 0x5F, 0x1F, 0x04, 0x00, 0x40, 0x1E, 0x5D,
                          0x04, 0x00, 0x00, 0x07])
            body += bytes([0x88, 0x02, 0x07, 0x80,
                           0x89, 0x07, 0x60, 0x85, 0x74, 0x05, 0x08, 0x02, 0x01,
                           0xBE, len(init) + 2, 0x04, len(init)]) + init
        return bytes([0x61, len(body)]) + body

    def _get(self, apdu):
        invoke = apdu[2]
        class_id = (apdu[3] << 8) | apdu[4]
        ln = bytes(apdu[5:11])
        attr = apdu[11]
        name = ".".join(str(b) for b in ln)
        if not self.associated:
            value, err = None, READ_WRITE_DENIED
        else:
            attrs = OBJECTS.get((class_id, ln))
            if attrs is None:
                value, err = None, OBJECT_UNDEFINED
            elif attr not in attrs:
                value, err = None, READ_WRITE_DENIED
            else:
                value = attrs[attr]
                value = value() if callable(value) else value
                err = 0
        self.log("SIM: GET {} {}/{} -> {}".format(class_id, name, attr, "ok" if value else "error " + str(err)))
        if value:
            return bytes([0xC4, 0x01, invoke, 0x00]) + value
        return bytes([0xC4, 0x01, invoke, 0x01, err])
