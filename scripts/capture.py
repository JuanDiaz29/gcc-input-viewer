#!/usr/bin/env python3

import argparse
import re
import shutil
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gc_decode as gc  # noqa: E402

SIGROK_PATH = [
    r"C:\Program Files\sigrok\sigrok-cli\sigrok-cli.exe",
    r"C:\Program Files (x86)\sigrok\sigrok-cli\sigrok-cli.exe",
]


def find_sigrok(explicit):
    if explicit:
        return explicit
    found = shutil.which("sigrok-cli")
    if found:
        return found
    for i in SIGROK_PATH:
        if Path(i).exists():
            return i
    sys.exit("error: sigrok-cli not found. Install it from sigrok.org (Downloads) "
             "or pass its location with --sigrok")


def clean_name(name):
    return re.sub(r'[<>:"/\\|?*\s]+', "_", name.strip()).strip("_")


def run_capture(args, sigrok, path):
    """Run sigrok-cli; return None on success or an error message."""
    cmd = [sigrok, "-d", args.driver,
           "-c", f"samplerate={args.samplerate}",
           "-C", args.channel,
           "--samples", args.samples,
           "-t", f"{args.channel}=f", "-w",
           "-O", "vcd", "-o", str(path)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=args.timeout)
    except subprocess.TimeoutExpired:
        path.unlink(missing_ok=True)
        return (f"no falling edge on {args.channel} within {args.timeout:g} s. "
                "Check that the adapter is plugged in and the analyzer is wired to the data line.")
    except OSError as e:
        return f"could not start sigrok-cli: {e}"
    if result.returncode != 0 or not path.exists() or path.stat().st_size == 0:
        message = (result.stderr or result.stdout).strip() or f"sigrok-cli exited with code {result.returncode}"
        if "claim" in message.lower() or "busy" in message.lower() or "no device" in message.lower():
            message += "\n(Is PulseView still open? Close it so sigrok-cli can use the analyzer.)"
        return message
    return None


def summarize(path, channel):
    transitions, _, capture_end = gc.parse_vcd(str(path), channel)
    transitions, glitches = gc.deglitch(transitions, 0.3)
    bursts = [gc.decode_burst(b, 2.0) for b in gc.split_bursts(gc.low_pulses(transitions), 20.0)]
    polls = [b for b in bursts if b["status"] == "ok" and b["name"] == "poll"]
    starts = [b["start"] for b in bursts]
    intervals = [b - a for a, b in zip(starts, starts[1:])]

    line = f"  {len(bursts)} bursts in {capture_end / 1000:.1f} ms, {len(polls)} complete polls"
    if intervals:
        line += f", {min(intervals):.0f}-{max(intervals):.0f} us apart"
    print(line)
    if glitches:
        print(f"  {glitches} glitch(es) ignored; check the analyzer ground if this keeps happening")

    commands = Counter(gc.hexs(b["command"]) or "(partial)" for b in bursts)
    print("  commands seen: " + ", ".join(f"{c} x{n}" for c, n in commands.most_common()))

    if not polls:
        print("  no complete poll replies to show")
        return
    replies = Counter(gc.hexs(b["reply"]) for b in polls)
    rows = []
    for reply, count in replies.most_common(5):
        fields = gc.decode_inputs(next(b for b in polls if gc.hexs(b["reply"]) == reply))
        rows.append([reply, count, " ".join(fields["buttons"]) or "none",
                     fields["stick_x"], fields["stick_y"], fields["cstick_x"], fields["cstick_y"],
                     fields["l_analog"], fields["r_analog"]])
    headers = ["reply (hex)", "polls", "buttons", "stick X", "stick Y", "C-stick X", "C-stick Y", "L", "R"]
    aligns = ["l", "r", "l", "r", "r", "r", "r", "r", "r"]
    print()
    print("\n".join(gc.table_lines(headers, rows, aligns)))
    if len(replies) > 5:
        print(f"  ... {len(replies) - 5} more distinct replies (run gc_decode.py for all of them)")


def target_path(args, name):
    return Path(args.outdir) / f"{args.prefix}{name}.vcd"


class SessionLog:
    """Stands in for sys.stdout: everything printed goes to the terminal and into the log."""

    def __init__(self, stream):
        self.stream = stream
        self.parts = []

    def write(self, text):
        self.stream.write(text)
        self.parts.append(text)
        return len(text)

    def flush(self):
        self.stream.flush()

    def note(self, text):
        """Add text to the log only (what the user typed, which the terminal already shows)."""
        self.parts.append(text)

    def text(self):
        return "".join(self.parts)


def ask(prompt):
    try:
        answer = input(prompt)
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if isinstance(sys.stdout, SessionLog):
        sys.stdout.note(answer + "\n")
    return answer


def capture_one(args, sigrok, name, interactive):
    path = target_path(args, name)
    if path.exists() and not args.force:
        if not interactive:
            sys.exit(f"error: {path} already exists (use --force to overwrite)")
        answer = ask(f"{path} exists. Overwrite? [y/N] ")
        if not answer or answer.strip().lower() != "y":
            print("Skipped.")
            return
    path.parent.mkdir(parents=True, exist_ok=True)

    if args.delay > 0:
        print("Hold the input. Capturing in", end="", flush=True)
        for s in range(int(args.delay), 0, -1):
            print(f" {s}...", end="", flush=True)
            time.sleep(1)
        print(" now", flush=True)

    error = run_capture(args, sigrok, path)
    if error:
        print(f"Capture failed: {error}")
        return

    print(f"Capture   {name}   {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"Saved {path}")
    try:
        summarize(path, args.channel)
    except ValueError as e:
        print(f"  could not decode: {e}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Capture the GameCube data line to a named VCD and decode it.")
    ap.add_argument("name", nargs="?", help="capture name; omit to be asked before each capture")
    ap.add_argument("--outdir", default="vcd_cap", help="folder for VCD files (default vcd_cap)")
    ap.add_argument("--prefix", default="gcc_", help="file name prefix (default gcc_)")
    ap.add_argument("--samplerate", default="8m", help="sample rate (default 8m = 8 MHz)")
    ap.add_argument("--samples", default="1m", help="samples per capture (default 1m = 125 ms at 8 MHz)")
    ap.add_argument("--channel", default="D0", help="analyzer channel on the data line (default D0)")
    ap.add_argument("--driver", default="fx2lafw", help="sigrok driver (default fx2lafw, used by Saleae-style clones)")
    ap.add_argument("--delay", type=float, default=2, help="countdown before each capture, s (default 2; 0 = none)")
    ap.add_argument("--timeout", type=float, default=10, help="give up if no falling edge arrives, s (default 10)")
    ap.add_argument("--sigrok", help="path to sigrok-cli if it is not on PATH")
    ap.add_argument("--force", action="store_true", help="overwrite existing files without asking")
    ap.add_argument("--log", metavar="FILE",
                    help="session log path (default <outdir>\\capture_log_<date>_<time>.txt; "
                         "written when you quit with a blank name)")
    args = ap.parse_args(argv)

    sigrok = find_sigrok(args.sigrok)

    if args.name:
        name = clean_name(args.name)
        if not name:
            sys.exit("error: empty capture name")
        capture_one(args, sigrok, name, interactive=False)
        return

    started = datetime.now()
    log_path = Path(args.log) if args.log else \
        Path(args.outdir) / f"capture_log_{started:%Y-%m-%d_%H%M%S}.txt"
    log = SessionLog(sys.stdout)
    sys.stdout = log
    names = []
    try:
        print(f"Captures go to {Path(args.outdir) / (args.prefix + '<name>.vcd')}. "
              f"Blank name quits and saves this session's output to {log_path}.")
        while True:
            raw = ask("\nCapture name: ")
            if raw is None or not raw.strip():
                break
            name = clean_name(raw)
            if not name:
                print("Use letters, numbers, - or _.")
                continue
            names.append(name)
            try:
                capture_one(args, sigrok, name, interactive=True)
            except KeyboardInterrupt:
                print("\nCapture interrupted. Quitting.")
                break
    finally:
        sys.stdout = log.stream
        if names:
            ended = datetime.now()
            header = (f"Capture session {started:%Y-%m-%d %H:%M:%S} to {ended:%H:%M:%S}, "
                      f"{len(names)} capture{'s' if len(names) != 1 else ''}: {', '.join(names)}\n"
                      f"{'=' * 78}\n\n")
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(header + log.text(), encoding="utf-8")
            print(f"Saved session output to {log_path}")
        else:
            print("No captures this session, so no log was written.")


if __name__ == "__main__":
    main()
