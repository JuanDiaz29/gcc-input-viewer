#!/usr/bin/env python3


import argparse
import csv
import re
import sys
from statistics import mean

UNIT_TO_US = {"s": 1e6, "ms": 1e3, "us": 1.0, "ns": 1e-3, "ps": 1e-6, "fs": 1e-9}

# command byte -> (name, command length in bytes, reply length in bytes)
COMMANDS = {
    0x40: ("poll", 3, 8),
    0x41: ("origin", 1, 10),
    0x42: ("calibrate", 3, 10),
    0x00: ("identify", 1, 3),
    0xFF: ("reset", 1, 3),
}

BYTE0_BUTTONS = [(0x10, "Start"), (0x08, "Y"), (0x04, "X"), (0x02, "B"), (0x01, "A")]
BYTE1_BUTTONS = [(0x40, "L"), (0x20, "R"), (0x10, "Z"),
                 (0x08, "D-Up"), (0x04, "D-Down"), (0x02, "D-Right"), (0x01, "D-Left")]

CSV_BUTTON_COLUMNS = ["A", "B", "X", "Y", "Start", "Z", "L_click", "R_click",
                      "D_up", "D_down", "D_right", "D_left"]


# --------------------------------------------------------------------------- VCD

def parse_vcd(path, channel):
    """Return (transitions, channel_name, capture_end_us).

    transitions is a list of (time_us, level) with level 0/1, one entry per change.
    """
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()

    head, sep, body = text.partition("$enddefinitions")
    if not sep:
        raise ValueError("not a VCD file (no $enddefinitions)")

    m = re.search(r"\$timescale\s+(\d+)\s*(s|ms|us|ns|ps|fs)\s+\$end", head)
    if not m:
        raise ValueError("no $timescale found in the VCD header")
    tick_us = int(m.group(1)) * UNIT_TO_US[m.group(2)]

    variables = re.findall(r"\$var\s+\S+\s+(\d+)\s+(\S+)\s+(\S+)[^$]*\$end", head)
    if not variables:
        raise ValueError("no $var definitions found in the VCD header")
    one_bit = [(ident, name) for size, ident, name in variables if size == "1"]
    match = [(i, n) for i, n in one_bit if n.lower() == channel.lower()]
    if match:
        ident, name = match[0]
    elif len(one_bit) == 1:
        ident, name = one_bit[0]
        print(f"note: channel {channel!r} not in VCD, using its only channel {name!r}", file=sys.stderr)
    else:
        names = ", ".join(n for _, n in one_bit)
        raise ValueError(f"channel {channel!r} not in VCD; channels found: {names}")

    transitions = []
    now = 0
    last_level = None
    last_time = 0
    tokens = body.split()
    i = 1  # skip the "$end" that closes $enddefinitions
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("#"):
            now = int(tok[1:])
            last_time = now
        elif tok == "$comment":
            while i < len(tokens) and tokens[i] != "$end":
                i += 1
        elif tok.startswith("$"):
            pass  # $dumpvars, $end, ...
        elif tok[0] in "bBrR":
            i += 1  # vector value: the next token is its identifier
        elif tok[0] in "01xXzZ" and tok[1:] == ident:
            level = 1 if tok[0] == "1" else 0
            if level != last_level:
                transitions.append((now * tick_us, level))
                last_level = level
        i += 1

    if not transitions:
        raise ValueError(f"no data for channel {name!r}")
    return transitions, name, last_time * tick_us


# ------------------------------------------------------------------ pulses/bursts

def deglitch(transitions, min_us):
    """Drop any high or low shorter than min_us (ringing, noise). Returns (clean, removed)."""
    out = []
    removed = 0
    for t, level in transitions:
        if len(out) >= 2 and t - out[-1][0] < min_us:
            out.pop()       # the segment that just started was too short: undo its edge
            removed += 1    # and this edge returns to the level we were already at
            continue
        out.append((t, level))
    return out, removed


def low_pulses(transitions):
    """Return a list of (fall_us, width_us, rise_us) for every complete low pulse.

    If the capture starts low (a falling-edge trigger with no pre-trigger samples),
    the first low is taken to start at the first sample.
    """
    lows = []
    fall = None
    prev = None
    for t, level in transitions:
        if level == 0 and (prev == 1 or prev is None):
            fall = t
        elif level == 1 and prev == 0 and fall is not None:
            lows.append((fall, t - fall, t))
            fall = None
        prev = level
    return lows


def split_bursts(lows, gap_us):
    """Group low pulses into bursts separated by more than gap_us of high."""
    bursts = []
    current = []
    for pulse in lows:
        if current and pulse[0] - current[-1][2] > gap_us:
            bursts.append(current)
            current = []
        current.append(pulse)
    if current:
        bursts.append(current)
    return bursts


def to_byte(bits):
    value = 0
    for b in bits:
        value = (value << 1) | b
    return value


def periods(falls):
    return [b - a for a, b in zip(falls, falls[1:])]


def guess_split(lows):
    """For an unknown command, find the command/reply split whose bit periods
    differ the most (adapter bits ~5 us, controller bits ~4 us)."""
    n = len(lows)
    falls = [p[0] for p in lows]
    best = None
    c = 1
    while 8 * c + 1 <= n:
        reply_bits = n - (8 * c + 1) - 1
        if n == 8 * c + 1:
            candidate = (c, 0)
        elif reply_bits >= 8 and reply_bits % 8 == 0:
            candidate = (c, reply_bits // 8)
        else:
            candidate = None
        if candidate:
            cmd_p = periods(falls[0:8 * c])
            rep_p = periods(falls[8 * c + 1:n - 1]) if candidate[1] else []
            score = (mean(cmd_p) if cmd_p else 0) - (mean(rep_p) if rep_p else 0)
            if best is None or score > best[0]:
                best = (score, candidate)
        c += 1
    return best[1] if best else None


def decode_burst(lows, threshold):
    """Decode one burst. Returns a dict describing it."""
    n = len(lows)
    bits = [1 if width < threshold else 0 for _, width, _ in lows]
    result = {
        "start": lows[0][0], "end": lows[-1][2], "n_lows": n, "lows": lows, "bits": bits,
        "status": "ok", "name": "?", "cmd_len": 0, "reply_len": 0,
        "command": [], "reply": [],
    }
    if n < 9:
        result["status"] = "partial"
        return result

    first = to_byte(bits[0:8])
    split = None
    if first in COMMANDS:
        name, clen, rlen = COMMANDS[first]
        if n == 8 * clen + 1 + 8 * rlen + 1:
            split = (clen, rlen)
        elif n == 8 * clen + 1:
            split = (clen, 0)
            result["status"] = "no reply"
        if split:
            result["name"] = name
    if split is None:
        split = guess_split(lows)
        if split is None:
            result["status"] = "partial"
            return result
        result["status"] = "guessed split" if split[1] else "no reply (guessed)"
        result["name"] = COMMANDS.get(first, ("unknown",))[0]

    clen, rlen = split
    result["cmd_len"], result["reply_len"] = clen, rlen
    result["command"] = [to_byte(bits[8 * k:8 * k + 8]) for k in range(clen)]
    base = 8 * clen + 1
    result["reply"] = [to_byte(bits[base + 8 * k:base + 8 * k + 8]) for k in range(rlen)]
    return result


def decode_inputs(burst):
    """Return a dict of named fields for poll/origin replies, else None."""
    reply = burst["reply"]
    if burst["name"] not in ("poll", "origin", "calibrate") or len(reply) < 8:
        return None
    b0, b1 = reply[0], reply[1]
    pressed = [label for mask, label in BYTE0_BUTTONS if b0 & mask]
    pressed += [label for mask, label in BYTE1_BUTTONS if b1 & mask]
    fields = {
        "buttons": pressed,
        "status_bits": b0 >> 5,
        "always1": bool(b1 & 0x80),
        "stick_x": reply[2], "stick_y": reply[3],
        "cstick_x": reply[4], "cstick_y": reply[5],
        "l_analog": reply[6], "r_analog": reply[7],
        "rumble": None, "mode": None,
    }
    if burst["name"] == "poll" and len(burst["command"]) == 3:
        fields["mode"] = burst["command"][1]
        fields["rumble"] = burst["command"][2] & 0x01
    return fields


# ------------------------------------------------------------------------ output

def hexs(values):
    return " ".join(f"{v:02X}" for v in values)


REPLY_FIELDS = ["buttons", "L R Z D-pad", "stick X", "stick Y", "C-stick X",
                "C-stick Y", "L analog", "R analog", "extra", "extra"]
POLL_CMD_FIELDS = ["command", "mode", "rumble"]

SUMMARY_HEADERS = ["#", "start us", "period us", "frame us", "command", "reply",
                   "buttons", "stick X", "stick Y", "C-stick X", "C-stick Y", "L", "R", "notes"]
SUMMARY_ALIGN = ["r", "r", "r", "r", "l", "l", "l", "r", "r", "r", "r", "r", "r", "l"]

BITS_HEADERS = ["byte", "field", "hex", "dec", "bits", "low widths (us)"]
BITS_ALIGN = ["l", "l", "r", "r", "l", "l"]

STATS_HEADERS = ["measurement", "min us", "avg us", "max us", "n"]
STATS_ALIGN = ["l", "r", "r", "r", "r"]

COLUMN_GAP = "   "


def column_widths(headers, rows):
    return [max([len(str(h))] + [len(str(r[i])) for r in rows]) for i, h in enumerate(headers)]


def table_lines(headers, rows, aligns, widths=None, indent=2):
    """Fixed-width table: header, dashed rule, rows; every column padded to its widest cell."""
    widths = widths or column_widths(headers, rows)
    pad = " " * indent

    def line(cells):
        out = [str(c).rjust(w) if a == "r" else str(c).ljust(w)
               for c, w, a in zip(cells, widths, aligns)]
        return (pad + COLUMN_GAP.join(out)).rstrip()

    return [line(headers), pad + COLUMN_GAP.join("-" * w for w in widths)] + [line(r) for r in rows]


def notes_text(burst, fields):
    if burst["status"] == "partial":
        return f"partial/unrecognized burst ({burst['n_lows']} lows)"
    notes = []
    if burst["name"] != "poll":
        notes.append(burst["name"])
    if burst["status"] != "ok":
        notes.append(burst["status"])
    if fields:
        if fields["rumble"]:
            notes.append("rumble on")
        if fields["mode"] not in (None, 0x03):
            notes.append(f"mode {fields['mode']:#04x}: analog layout differs")
        if fields["status_bits"]:
            notes.append(f"status bits {fields['status_bits']:03b}")
        if not fields["always1"]:
            notes.append("byte1 bit7 is 0 (check capture)")
    return "; ".join(notes)


def summary_row(idx, burst, period, fields):
    row = [idx, f"{burst['start']:.2f}", "-" if period is None else f"{period:.2f}",
           f"{burst['end'] - burst['start']:.2f}",
           hexs(burst["command"]) or "-", hexs(burst["reply"]) or "-"]
    if fields:
        row += [" ".join(fields["buttons"]) or "none",
                fields["stick_x"], fields["stick_y"], fields["cstick_x"], fields["cstick_y"],
                fields["l_analog"], fields["r_analog"]]
    else:
        row += ["-", "", "", "", "", "", ""]
    row.append(notes_text(burst, fields))
    return row


def full_hex_line(burst):
    """The whole transaction on one line: spaced bytes, then each message as one hex value."""
    cmd, reply = burst["command"], burst["reply"]
    if not cmd:
        return f"  hex   - ({notes_text(burst, None)})"
    spaced = hexs(cmd) + (" | " + hexs(reply) if reply else " | no reply")
    joined = f"command 0x{''.join(f'{v:02X}' for v in cmd)}"
    if reply:
        joined += f", reply 0x{''.join(f'{v:02X}' for v in reply)}"
    return f"  hex   {spaced}   ({joined})"


def bits_rows(burst):
    """One row per byte, stop bit and gap, for the --bits view."""
    lows, bits = burst["lows"], burst["bits"]

    def widths(i, j):
        return "  ".join(f"{lows[k][1]:.2f}" for k in range(i, j))

    def byte_row(label, field, i):
        seg = bits[i:i + 8]
        value = to_byte(seg)
        s = "".join(map(str, seg))
        return [label, field, f"{value:02X}", value, f"{s[:4]} {s[4:]}", widths(i, i + 8)]

    if not burst["cmd_len"]:
        return [["raw", f"{len(lows)} lows", "", "", "", widths(0, len(lows))]]

    rows = []
    is_poll = burst["name"] == "poll" and burst["cmd_len"] == 3
    for k in range(burst["cmd_len"]):
        field = POLL_CMD_FIELDS[k] if is_poll else ("command" if k == 0 else "")
        rows.append(byte_row(f"cmd[{k}]", field, 8 * k))
    stop = 8 * burst["cmd_len"]
    rows.append(["cmd stop", "stop bit", "", "", "", f"{lows[stop][1]:.2f}"])
    if burst["reply_len"]:
        gap = lows[stop + 1][0] - lows[stop][2]
        rows.append(["gap", "high before reply", "", "", "", f"{gap:.2f}"])
        known_layout = burst["name"] in ("poll", "origin", "calibrate")
        for k in range(burst["reply_len"]):
            field = REPLY_FIELDS[k] if known_layout and k < len(REPLY_FIELDS) else ""
            rows.append(byte_row(f"reply[{k}]", field, stop + 1 + 8 * k))
        rows.append(["reply stop", "stop bit", "", "", "", f"{lows[-1][1]:.2f}"])
    return rows


def stats_groups(bursts):
    groups = {k: [] for k in ("cmd1", "cmd0", "cmdstop", "rep1", "rep0", "repstop",
                              "cmdper", "repper", "gap", "frame")}
    for b in bursts:
        if not b["cmd_len"]:
            continue
        lows, bits = b["lows"], b["bits"]
        stop = 8 * b["cmd_len"]
        for i in range(stop):
            groups["cmd1" if bits[i] else "cmd0"].append(lows[i][1])
        groups["cmdstop"].append(lows[stop][1])
        groups["cmdper"] += periods([p[0] for p in lows[0:stop + 1]])
        if b["reply_len"]:
            for i in range(stop + 1, len(lows) - 1):
                groups["rep1" if bits[i] else "rep0"].append(lows[i][1])
            groups["repstop"].append(lows[-1][1])
            groups["repper"] += periods([p[0] for p in lows[stop + 1:]])
            groups["gap"].append(lows[stop + 1][0] - lows[stop][2])
            if b["name"] == "poll" and b["reply_len"] == 8:
                groups["frame"].append(b["end"] - b["start"])
    return groups


def stats_rows(bursts, intervals):
    g = stats_groups(bursts)
    items = [
        ("time between bursts", intervals),
        ("poll frame length", g["frame"]),
        ("command '1' low", g["cmd1"]),
        ("command '0' low", g["cmd0"]),
        ("command stop low", g["cmdstop"]),
        ("command bit period", g["cmdper"]),
        ("gap before reply", g["gap"]),
        ("reply '1' low", g["rep1"]),
        ("reply '0' low", g["rep0"]),
        ("reply stop low", g["repstop"]),
        ("reply bit period", g["repper"]),
    ]
    rows = []
    for label, values in items:
        if values:
            rows.append([label, f"{min(values):.2f}", f"{mean(values):.2f}", f"{max(values):.2f}", len(values)])
        else:
            rows.append([label, "-", "-", "-", 0])
    return rows


def write_csv(path, rows):
    header = ["burst", "start_us", "period_us", "frame_us", "n_lows", "status", "command_name",
              "command_hex", "reply_hex"] + CSV_BUTTON_COLUMNS + [
              "status_bits", "stick_x", "stick_y", "cstick_x", "cstick_y",
              "l_analog", "r_analog", "rumble"]
    names = {"A": "A", "B": "B", "X": "X", "Y": "Y", "Start": "Start", "Z": "Z",
             "L_click": "L", "R_click": "R", "D_up": "D-Up", "D_down": "D-Down",
             "D_right": "D-Right", "D_left": "D-Left"}
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        for idx, burst, period, fields in rows:
            line = [idx, f"{burst['start']:.3f}", "" if period is None else f"{period:.3f}",
                    f"{burst['end'] - burst['start']:.3f}", burst["n_lows"], burst["status"],
                    burst["name"], hexs(burst["command"]), hexs(burst["reply"])]
            if fields:
                line += [int(names[c] in fields["buttons"]) for c in CSV_BUTTON_COLUMNS]
                line += [fields["status_bits"], fields["stick_x"], fields["stick_y"],
                         fields["cstick_x"], fields["cstick_y"], fields["l_analog"],
                         fields["r_analog"], "" if fields["rumble"] is None else fields["rumble"]]
            else:
                line += [""] * (len(CSV_BUTTON_COLUMNS) + 8)
            w.writerow(line)


# -------------------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(description="Decode GameCube controller traffic from a PulseView VCD export.")
    ap.add_argument("vcd", help="VCD file exported from PulseView")
    ap.add_argument("--start", type=float, default=None, help="window start in us (burst start time)")
    ap.add_argument("--end", type=float, default=None, help="window end in us (burst start time)")
    ap.add_argument("--channel", default="D0", help="channel name in the VCD (default D0)")
    ap.add_argument("--threshold", type=float, default=2.0, help="low width separating 1 from 0, us (default 2.0)")
    ap.add_argument("--gap", type=float, default=20.0, help="high time that ends a burst, us (default 20)")
    ap.add_argument("--glitch", type=float, default=0.3,
                    help="ignore highs/lows shorter than this, us (default 0.3; 0 disables)")
    ap.add_argument("--bits", action="store_true", help="print every bit and low width")
    ap.add_argument("--csv", metavar="FILE", help="write one row per burst to a CSV file")
    args = ap.parse_args(argv)

    try:
        transitions, name, capture_end = parse_vcd(args.vcd, args.channel)
    except (OSError, ValueError) as e:
        sys.exit(f"error: {e}")

    glitches = 0
    if args.glitch > 0:
        transitions, glitches = deglitch(transitions, args.glitch)
    bursts = [decode_burst(b, args.threshold) for b in split_bursts(low_pulses(transitions), args.gap)]
    start = args.start if args.start is not None else float("-inf")
    end = args.end if args.end is not None else float("inf")

    rows = []
    prev_start = None
    for idx, b in enumerate(bursts):
        period = None if prev_start is None else b["start"] - prev_start
        prev_start = b["start"]
        if start <= b["start"] <= end:
            rows.append((idx, b, period, decode_inputs(b)))

    window = (f"{'capture start' if args.start is None else f'{args.start:g} us'} to "
              f"{'capture end' if args.end is None else f'{args.end:g} us'}")
    info = [("File", args.vcd),
            ("Channel", name),
            ("Capture", f"{capture_end:.1f} us, {len(bursts)} bursts"),
            ("Window", f"{window}, {len(rows)} burst{'s' if len(rows) != 1 else ''}")]
    if glitches:
        info.append(("Glitches", f"{glitches} ignored (shorter than {args.glitch:g} us; "
                                 "check the probe ground if this is large)"))
    for key, value in info:
        print(f"{key:<10}{value}")
    if not rows:
        return

    summary = [summary_row(idx, b, period, fields) for idx, b, period, fields in rows]
    widths = column_widths(SUMMARY_HEADERS, summary)
    if args.bits:
        for (idx, b, _, _), srow in zip(rows, summary):
            print(f"\nBurst {idx}")
            print(full_hex_line(b))
            print()
            print("\n".join(table_lines(SUMMARY_HEADERS, [srow], SUMMARY_ALIGN, widths)))
            print()
            print("\n".join(table_lines(BITS_HEADERS, bits_rows(b), BITS_ALIGN, indent=6)))
    else:
        print("\nBursts")
        print("\n".join(table_lines(SUMMARY_HEADERS, summary, SUMMARY_ALIGN, widths)))

    intervals = [p for _, b, p, _ in rows if p is not None]
    print("\nTiming")
    print("\n".join(table_lines(STATS_HEADERS, stats_rows([b for _, b, _, _ in rows], intervals), STATS_ALIGN)))

    if args.csv:
        write_csv(args.csv, rows)
        print(f"\nWrote {len(rows)} rows to {args.csv}")


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:  # output piped into head/more and closed early
        import os
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(1)
