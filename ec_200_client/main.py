# =============================================================================
#  Sample 1 : Network status check  (QuecPython)
#  Shows    : module info, SIM status, signal, registration, operator,
#             data call (IP address), DNS + internet reachability,
#             and live network up/down events.
#  Run      : upload as /usr/net_info.py, then in REPL:  import net_info
# =============================================================================

import utime
import modem
import sim
import net
import dataCall
import checkNet
import usocket


def line(title):
    print("\n==== %s ====" % title)


def safe(fn, *args):
    """Some APIs differ per module series -> never let one failure stop the test."""
    try:
        return fn(*args)
    except Exception as e:
        return "N/A (%s)" % e


# ----------------------------------------------------------------------------
# 1. Module identity
# ----------------------------------------------------------------------------
def module_info():
    line("1. MODULE")
    print("IMEI      :", safe(modem.getDevImei))
    print("Model     :", safe(modem.getDevModel))
    print("Firmware  :", safe(modem.getDevFwVersion))


# ----------------------------------------------------------------------------
# 2. SIM card
# ----------------------------------------------------------------------------
SIM_STATE = {0: "NOT INSERTED", 1: "READY", 2: "PIN REQUIRED", 3: "PUK REQUIRED"}


def sim_info(wait_s=20):
    line("2. SIM")
    # At boot the SIM needs a few seconds to initialise -> poll instead of reading once
    st = safe(sim.getStatus)
    t0 = utime.time()
    while st != 1 and utime.time() - t0 < wait_s:
        utime.sleep(1)
        st = safe(sim.getStatus)
    print("Waited    : %d s" % (utime.time() - t0))
    print("Status    :", st, "->", SIM_STATE.get(st, "other/unknown"))
    if st == 1:
        print("IMSI      :", safe(sim.getImsi))
        print("ICCID     :", safe(sim.getIccid))
    return st == 1


# ----------------------------------------------------------------------------
# 3. Signal quality
# ----------------------------------------------------------------------------
def csq_text(csq):
    if not isinstance(csq, int) or csq == 99:
        return "unknown / no signal"
    dbm = -113 + 2 * csq
    q = "excellent" if csq >= 20 else "good" if csq >= 15 else "fair" if csq >= 10 else "poor"
    return "%d dBm (%s)" % (dbm, q)


def signal_info():
    line("3. SIGNAL")
    csq = safe(net.csqQueryPoll)
    print("CSQ       :", csq, "->", csq_text(csq))
    sig = safe(net.getSignal)               # ([gsm...], [lte rssi, rsrp, rsrq, cqi])
    if isinstance(sig, tuple) and len(sig) > 1 and sig[1]:
        lte = sig[1]
        print("LTE RSRP  :", lte[1], "dBm   (> -100 good, < -110 weak)")
        print("LTE RSRQ  :", lte[2], "dB    (> -10 good, < -15 weak)")
    else:
        print("Raw       :", sig)


# ----------------------------------------------------------------------------
# 4. Network registration + operator
# ----------------------------------------------------------------------------
REG_STATE = {0: "not registered", 1: "registered (home)", 2: "searching",
             3: "denied", 4: "unknown", 5: "registered (roaming)"}
ACT = {0: "GSM", 2: "UTRAN", 7: "LTE", 8: "LTE-M", 9: "NB-IoT"}   # varies slightly per series


def reg_info():
    line("4. REGISTRATION")
    st = safe(net.getState)                  # ([voice...], [stat, lac, cid, act, ...])
    registered = False
    if isinstance(st, tuple) and len(st) > 1 and st[1]:
        d = st[1]
        print("Data reg  :", d[0], "->", REG_STATE.get(d[0], "?"))
        print("LAC/TAC   :", d[1], "  Cell ID:", d[2])
        print("Access    :", d[3], "->", ACT.get(d[3], "see docs"))
        registered = d[0] in (1, 5)
    else:
        print("Raw       :", st)
    op = safe(net.operatorName)              # (long, short, mcc, mnc)
    print("Operator  :", op)
    return registered


# ----------------------------------------------------------------------------
# 5. Data call -> IP address  (THE "am I online?" answer)
# ----------------------------------------------------------------------------
def ip_info():
    line("5. DATA CALL / IP")
    # Check profiles 1..3 and ask for IPv4v6 (ipType 2).
    # Result: [profile, ipType, [v4: state, reconn, ip, dns1, dns2], [v6: same...]]
    # Airtel/Jio LTE often give IPv6 or dual-stack, so IPv4-only query can show 0.0.0.0
    online = False
    for pid in (1, 2, 3):
        info = safe(dataCall.getInfo, pid, 2)
        print("Profile %d raw:" % pid, info)
        if not isinstance(info, (list, tuple)) or info == -1:
            continue
        for i, part in enumerate(info[2:]):
            if not isinstance(part, (list, tuple)) or len(part) < 3:
                continue
            fam = "IPv4" if i == 0 else "IPv6"
            up = part[0] == 1
            print("  %s      : %s  ip=%s" % (fam, "CONNECTED" if up else "down", part[2]))
            if up:
                online = True
    return online


# ----------------------------------------------------------------------------
# 6. Real internet test: DNS lookup + TCP connect
# ----------------------------------------------------------------------------
def internet_test(host="www.google.com", port=80):
    line("6. INTERNET TEST")
    t0 = utime.ticks_ms()
    try:
        ai = usocket.getaddrinfo(host, port)[0]      # (family, type, proto, canon, sockaddr)
        family, addr = ai[0], ai[-1]
        fam_txt = "IPv6" if family == usocket.AF_INET6 else "IPv4"
        print("DNS       : %s -> %s (%s)" % (host, addr[0], fam_txt))
    except Exception as e:
        print("DNS       : FAILED", e)
        return False
    # Socket family MUST match the address family, else Errno 22 EINVAL
    s = usocket.socket(family, usocket.SOCK_STREAM)
    try:
        s.settimeout(10)
        s.connect(addr)
        ms = utime.ticks_diff(utime.ticks_ms(), t0)
        print("TCP       : connected to %s:%d in %d ms" % (addr[0], port, ms))
        return True
    except Exception as e:
        print("TCP       : FAILED", e)
        return False
    finally:
        s.close()


# ----------------------------------------------------------------------------
# 7. Live events: get notified when network drops / comes back
# ----------------------------------------------------------------------------
def on_net_event(args):
    # args[0] = profile id, args[1] = 1 connected / 0 disconnected
    print("\n[EVENT] profile %d -> %s" % (args[0], "UP" if args[1] == 1 else "DOWN"))


# ----------------------------------------------------------------------------
# RUN
# ----------------------------------------------------------------------------
def run():
    print("\n######## QuecPython Network Check ########")
    module_info()
    if not sim_info():
        print("\n>> STOP: insert/unlock SIM first")
        return

    line("WAITING FOR NETWORK (max 60 s)")
    stage, sub = checkNet.waitNetworkReady(60)
    print("checkNet  : stage=%d sub=%d -> %s" %
          (stage, sub, "READY" if (stage == 3 and sub == 1) else "NOT READY"))
    # stage 1 = SIM, 2 = registration, 3 = data call. Tells you WHERE it got stuck.

    signal_info()
    reg_info()
    online = ip_info()
    net_ready = (stage == 3 and sub == 1)
    if online or net_ready:            # trust checkNet too; always do the real test
        online = internet_test()

    safe(dataCall.setCallback, on_net_event)
    line("SUMMARY")
    print("ONLINE" if online else "OFFLINE", "- event monitor active (pull antenna to test)")


run()