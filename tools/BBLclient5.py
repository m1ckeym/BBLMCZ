#!/usr/bin/env python3
"""
mcz_monitor_client15.py

Serial client for the BBLMCZ/Zilog PROM monitor and MCZIMAGER companion.

Key features:
- Open serial port as 9600 7E1
- Safe monitor sync using '.' pre-sync trick, then 'r<CR>'
- Companion-aware sync using '.' pre-sync trick, then bare CR
- Conservative per-character TX pacing
- Optional RAM prefill workaround before upload
- Chunked monitor upload using d...q sessions
- Optional verify sample after load
- Optional jump to loaded companion
- Companion command helpers:
    Yxx -> yank one track as Intel HEX to local file
    Txx -> take one track from local Intel HEX file
    Uxx -> diagnostic receive one track, force track bytes, dump back as Intel HEX
    Vxx -> diagnostic receive, low-level write, PROM-read back, dump Intel HEX

New in this build:
- Live progress display while sending Intel-HEX lines for T/U/V
- Polls target output after each line
- Prints target output live during T/U/V send
- Stops sending immediately if the companion emits an error or returns to prompt
"""

from __future__ import annotations

import argparse
import queue
import re
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    import serial
except ImportError:
    print("error: pyserial is required. Install with: pip install pyserial", file=sys.stderr)
    raise


REGISTER_HEADER_RE = re.compile(
    r"\bA\s+B\s+C\s+D\s+E\s+F\s+H\s+L\s+I\b", re.IGNORECASE
)
PROMPT_MONITOR_RE = re.compile(r"(?:^|\r|\n)>\s*$", re.MULTILINE)

COMPANION_BANNER_RE = re.compile(r"BBLIMAGER|MCZIMAGER", re.IGNORECASE)
COMPANION_PROMPT_RE = re.compile(r"BBL>|IMAGER READY.*?>", re.IGNORECASE | re.DOTALL)

HEX4_RE = re.compile(r"(?<![0-9A-Fa-f])([0-9A-Fa-f]{4})(?![0-9A-Fa-f])")

COMPANION_ERROR_PATTERNS = [
    re.compile(r"CHECKSUM ERROR", re.IGNORECASE),
    re.compile(r"TRACK TOO LONG", re.IGNORECASE),
    re.compile(r"TRACK TOO SHORT", re.IGNORECASE),
    re.compile(r"BAD HEX RECORD TYPE", re.IGNORECASE),
    re.compile(r"BAD TRACK", re.IGNORECASE),
]

COMPANION_ABORT_PATTERN = re.compile(
    r"(CHECKSUM ERROR|TRACK TOO LONG|TRACK TOO SHORT|BAD HEX RECORD TYPE|BAD TRACK|IMAGER READY.*?>)",
    re.IGNORECASE | re.DOTALL,
)


@dataclass
class SyncResult:
    success: bool
    mode: str
    saw_prompt: bool
    saw_registers: bool
    transcript: str


@dataclass
class SendWatchResult:
    aborted: bool
    saw_prompt: bool
    reason: Optional[str]
    transcript: str


def extract_address_tokens(text: str) -> list[int]:
    out: list[int] = []
    for m in HEX4_RE.finditer(text):
        try:
            out.append(int(m.group(1), 16))
        except ValueError:
            pass
    return out


def extract_ihex_lines(text: str) -> list[str]:
    """
    Extract Intel-HEX records robustly from mixed console output.
    """
    norm = text.replace("\r", "\n")
    out: list[str] = []

    for raw_line in norm.split("\n"):
        line = raw_line.strip()
        if not line:
            continue

        colon = line.find(":")
        if colon >= 0:
            line = line[colon:].strip()

        if not line.startswith(":"):
            continue

        hexpart = line[1:]
        if len(hexpart) < 10:
            continue
        if len(hexpart) % 2 != 0:
            continue
        if all(c in "0123456789ABCDEFabcdef" for c in hexpart):
            out.append(line)

    return out


class SerialMonitorClient:
    def __init__(
        self,
        port: str,
        baudrate: int = 9600,
        timeout: float = 0.05,
        log_path: Optional[str] = None,
        tx_char_delay: float = 0.020,
        tx_line_delay: float = 0.200,
    ) -> None:
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.log_path = log_path
        self.tx_char_delay = tx_char_delay
        self.tx_line_delay = tx_line_delay

        self.ser: Optional[serial.Serial] = None
        self._rx_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._rx_queue: "queue.Queue[str]" = queue.Queue()
        self._log_fp = None

        self.current_mode = "monitor"

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def open(self) -> None:
        self.ser = serial.Serial(
            port=self.port,
            baudrate=self.baudrate,
            bytesize=serial.SEVENBITS,
            parity=serial.PARITY_EVEN,
            stopbits=serial.STOPBITS_ONE,
            timeout=self.timeout,
            write_timeout=2.0,
            xonxoff=False,
            rtscts=False,
            dsrdtr=False,
        )
        time.sleep(0.15)

        if self.log_path:
            self._log_fp = open(self.log_path, "a", encoding="utf-8", buffering=1)

        self._stop.clear()
        self._rx_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._rx_thread.start()

    def close(self) -> None:
        self._stop.set()

        if self._rx_thread and self._rx_thread.is_alive():
            self._rx_thread.join(timeout=0.5)

        if self._log_fp:
            self._log_fp.close()
            self._log_fp = None

        if self.ser:
            try:
                self.ser.close()
            finally:
                self.ser = None

    def _reader_loop(self) -> None:
        assert self.ser is not None
        while not self._stop.is_set():
            try:
                data = self.ser.read(256)
            except serial.SerialException as e:
                self._rx_queue.put(f"\n[serial read error: {e}]\n")
                return

            if not data:
                continue

            text = data.decode("ascii", errors="replace")
            self._rx_queue.put(text)

            if self._log_fp:
                self._log_fp.write(text)

    # ------------------------------------------------------------------
    # Mode detection
    # ------------------------------------------------------------------

    def _detect_mode_from_text(self, text: str) -> Optional[str]:
        if COMPANION_BANNER_RE.search(text) or COMPANION_PROMPT_RE.search(text):
            return "companion"
        if REGISTER_HEADER_RE.search(text) or PROMPT_MONITOR_RE.search(text) or ">" in text:
            return "monitor"
        return None

    def _update_mode_from_text(self, text: str) -> None:
        mode = self._detect_mode_from_text(text)
        if mode is not None:
            self.current_mode = mode

    # ------------------------------------------------------------------
    # RX helpers
    # ------------------------------------------------------------------

    def drain_output(self, quiet_for: float = 0.20, max_total: float = 1.0) -> str:
        deadline = time.monotonic() + max_total
        last_data = time.monotonic()
        pieces: list[str] = []

        while time.monotonic() < deadline:
            try:
                chunk = self._rx_queue.get(timeout=0.05)
                pieces.append(chunk)
                last_data = time.monotonic()
            except queue.Empty:
                if time.monotonic() - last_data >= quiet_for:
                    break

        text = "".join(pieces)
        self._update_mode_from_text(text)
        return text

    def read_until_quiet(self, quiet_for: float = 0.35, max_total: float = 3.0) -> str:
        deadline = time.monotonic() + max_total
        last_data = time.monotonic()
        pieces: list[str] = []

        while time.monotonic() < deadline:
            try:
                chunk = self._rx_queue.get(timeout=0.05)
                pieces.append(chunk)
                last_data = time.monotonic()
            except queue.Empty:
                if time.monotonic() - last_data >= quiet_for:
                    break

        text = "".join(pieces)
        self._update_mode_from_text(text)
        return text

    def poll_output_once(self, dwell: float = 0.08, max_total: float = 0.20) -> str:
        deadline = time.monotonic() + max_total
        last_data = None
        pieces: list[str] = []

        while time.monotonic() < deadline:
            timeout = min(0.05, max(0.0, deadline - time.monotonic()))
            try:
                chunk = self._rx_queue.get(timeout=timeout)
                pieces.append(chunk)
                last_data = time.monotonic()
            except queue.Empty:
                if last_data is not None and (time.monotonic() - last_data) >= dwell:
                    break

        text = "".join(pieces)
        if text:
            self._update_mode_from_text(text)
        return text

    def wait_for_any_prompt(self, timeout: float = 4.0) -> tuple[bool, str, str]:
        deadline = time.monotonic() + timeout
        pieces: list[str] = []
        buf = ""

        while time.monotonic() < deadline:
            try:
                chunk = self._rx_queue.get(timeout=0.05)
                pieces.append(chunk)
                buf += chunk
                mode = self._detect_mode_from_text(buf)
                if mode is not None:
                    self.current_mode = mode
                    return True, mode, "".join(pieces)
            except queue.Empty:
                continue

        return False, self.current_mode, "".join(pieces)

    def wait_for_companion_prompt(
        self,
        timeout: float = 25.0,
        quiet_for: float = 1.5,
    ) -> tuple[bool, str]:
        deadline = time.monotonic() + timeout
        last_data = time.monotonic()
        pieces: list[str] = []
        buf = ""
        saw_banner = False

        while time.monotonic() < deadline:
            try:
                chunk = self._rx_queue.get(timeout=0.05)
                pieces.append(chunk)
                buf += chunk
                last_data = time.monotonic()

                if COMPANION_BANNER_RE.search(buf):
                    saw_banner = True
                    self.current_mode = "companion"

                if COMPANION_PROMPT_RE.search(buf):
                    self.current_mode = "companion"
                    return True, "".join(pieces)

            except queue.Empty:
                if saw_banner and (time.monotonic() - last_data >= quiet_for):
                    self.current_mode = "companion"
                    return True, "".join(pieces)

        return saw_banner or bool(COMPANION_PROMPT_RE.search(buf)), "".join(pieces)

    def wait_for_address_progress(
        self,
        expected_addr: int,
        floor_addr: int,
        timeout: float = 5.0,
    ) -> tuple[bool, str, Optional[int]]:
        deadline = time.monotonic() + timeout
        pieces: list[str] = []
        buf = ""
        highest_seen: Optional[int] = None

        while time.monotonic() < deadline:
            try:
                chunk = self._rx_queue.get(timeout=0.05)
                pieces.append(chunk)
                buf += chunk

                for addr in extract_address_tokens(buf):
                    if addr < floor_addr:
                        continue
                    if addr > 0xFFFF:
                        continue
                    if highest_seen is None or addr > highest_seen:
                        highest_seen = addr

                if highest_seen is not None and highest_seen >= expected_addr:
                    return True, "".join(pieces), highest_seen

            except queue.Empty:
                continue

        return False, "".join(pieces), highest_seen

    # ------------------------------------------------------------------
    # TX helpers
    # ------------------------------------------------------------------

    def write_raw_paced(
        self,
        data: bytes,
        char_delay: Optional[float] = None,
        line_delay: Optional[float] = None,
    ) -> None:
        assert self.ser is not None

        cd = self.tx_char_delay if char_delay is None else char_delay
        ld = self.tx_line_delay if line_delay is None else line_delay

        for b in data:
            self.ser.write(bytes([b]))
            self.ser.flush()

            if cd > 0.0:
                time.sleep(cd)

            if b in (0x0D, 0x0A) and ld > 0.0:
                time.sleep(ld)

    def send_line(
        self,
        line: str,
        end: bytes = b"\r",
        char_delay: Optional[float] = None,
        line_delay: Optional[float] = None,
    ) -> None:
        payload = line.encode("ascii", errors="strict") + end
        self.write_raw_paced(payload, char_delay=char_delay, line_delay=line_delay)

    def send_safe_nop_probe(self) -> str:
        self.write_raw_paced(b".\r")
        return self.read_until_quiet(quiet_for=0.40, max_total=2.5)

    # ------------------------------------------------------------------
    # Sync helpers
    # ------------------------------------------------------------------

    def sync_monitor(self, tries: int = 3) -> SyncResult:
        transcript_parts: list[str] = []
        probe = self.send_safe_nop_probe()
        transcript_parts.append(probe)

        for _ in range(tries):
            self.send_line("r")
            out = self.read_until_quiet(quiet_for=0.40, max_total=3.0)
            transcript_parts.append(out)

            joined = "".join(transcript_parts)
            saw_regs = bool(REGISTER_HEADER_RE.search(joined))
            saw_prompt = bool(PROMPT_MONITOR_RE.search(joined) or ">" in joined)

            if saw_regs or saw_prompt:
                self.current_mode = "monitor"
                return SyncResult(True, "monitor", saw_prompt, saw_regs, joined)

            time.sleep(0.15)

        joined = "".join(transcript_parts)
        return SyncResult(
            False,
            "monitor",
            bool(PROMPT_MONITOR_RE.search(joined) or ">" in joined),
            bool(REGISTER_HEADER_RE.search(joined)),
            joined,
        )

    def sync_companion(self, tries: int = 2) -> SyncResult:
        transcript_parts: list[str] = []
        probe = self.send_safe_nop_probe()
        transcript_parts.append(probe)

        for _ in range(tries):
            self.write_raw_paced(b"\r")
            out = self.read_until_quiet(quiet_for=0.60, max_total=4.0)
            transcript_parts.append(out)

            joined = "".join(transcript_parts)
            saw_companion = bool(
                COMPANION_BANNER_RE.search(joined) or COMPANION_PROMPT_RE.search(joined)
            )

            if saw_companion:
                self.current_mode = "companion"
                return SyncResult(True, "companion", True, False, joined)

            time.sleep(0.15)

        joined = "".join(transcript_parts)
        return SyncResult(
            False,
            "companion",
            bool(COMPANION_PROMPT_RE.search(joined)),
            False,
            joined,
        )

    def sync_auto(self) -> SyncResult:
        return self.sync_companion() if self.current_mode == "companion" else self.sync_monitor()

    def require_monitor_heartbeat(self, tries: int = 3) -> tuple[bool, str]:
        res = self.sync_monitor(tries=tries)
        return res.success, res.transcript

    def send_command_and_read(
        self,
        command: str,
        settle: float = 0.40,
        max_total: float = 3.0,
    ) -> str:
        self.send_line(command)
        return self.read_until_quiet(quiet_for=settle, max_total=max_total)

    def send_companion_command(
        self,
        command: str,
        timeout: float = 25.0,
        quiet_for: float = 1.5,
    ) -> tuple[bool, str]:
        self.current_mode = "companion"
        _ = self.send_safe_nop_probe()
        self.send_line(command)
        ok, text = self.wait_for_companion_prompt(timeout=timeout, quiet_for=quiet_for)
        return ok, text

    # ------------------------------------------------------------------
    # PROM helpers
    # ------------------------------------------------------------------

    def prefill_memory_range(
        self,
        start_addr: int,
        end_addr: int,
        value: int,
        passes: int = 1,
        settle: float = 0.60,
        max_total: float = 6.0,
    ) -> bool:
        self.current_mode = "monitor"

        for n in range(passes):
            hb_ok, hb_text = self.require_monitor_heartbeat()
            if hb_text:
                sys.stdout.write(hb_text)
                sys.stdout.flush()
            if not hb_ok:
                print(f"error: no clean monitor heartbeat before prefill pass {n+1}", file=sys.stderr)
                return False

            cmd = f"f {start_addr:04X} {end_addr:04X} {value:02X}"
            out = self.send_command_and_read(cmd, settle=settle, max_total=max_total)
            if out:
                sys.stdout.write(out)
                sys.stdout.flush()

            hb_ok2, hb_text2 = self.require_monitor_heartbeat()
            if hb_text2:
                sys.stdout.write(hb_text2)
                sys.stdout.flush()
            if not hb_ok2:
                print(f"error: no clean monitor heartbeat after prefill pass {n+1}", file=sys.stderr)
                return False

        return True

    def enter_monitor_data_mode(self, base: int) -> tuple[bool, str]:
        self.current_mode = "monitor"
        _ = self.drain_output(quiet_for=0.10, max_total=0.25)

        self.send_line(f"d {base:04X}")
        ok, text, _highest = self.wait_for_address_progress(
            expected_addr=base,
            floor_addr=base,
            timeout=5.0,
        )
        return ok, text

    def close_monitor_data_mode(self, timeout: float = 5.0) -> tuple[bool, str]:
        self.send_line("q")
        ok, mode, text = self.wait_for_any_prompt(timeout=timeout)
        return ok and mode == "monitor", text

    def load_binary_via_monitor_chunked(
        self,
        bin_path: str,
        base: int,
        chunk_size: int = 0x400,
        report_every: int = 64,
        verbose: bool = True,
    ) -> bool:
        data = Path(bin_path).read_bytes()

        if verbose:
            print(f"Uploading {len(data)} bytes from {bin_path} to {base:04X}h")
            print(
                f"TX pacing: {self.tx_char_delay*1000:.1f} ms/char, "
                f"{self.tx_line_delay*1000:.1f} ms/newline"
            )
            print(f"Upload mode: chunked monitor entry, chunk_size={chunk_size:#x}")

        sent_total = 0
        next_report = report_every

        for chunk_off in range(0, len(data), chunk_size):
            chunk = data[chunk_off : chunk_off + chunk_size]
            chunk_base = base + chunk_off
            chunk_end = chunk_base + len(chunk) - 1

            if verbose:
                print(f"\nChunk {chunk_off//chunk_size:02d}: {chunk_base:04X}-{chunk_end:04X} ({len(chunk)} bytes)")

            hb_ok, hb_text = self.require_monitor_heartbeat()
            if hb_text:
                sys.stdout.write(hb_text)
                sys.stdout.flush()
            if not hb_ok:
                print(f"error: no clean monitor heartbeat before chunk at {chunk_base:04X}", file=sys.stderr)
                return False

            ok, prelude = self.enter_monitor_data_mode(chunk_base)
            if prelude:
                sys.stdout.write(prelude)
                sys.stdout.flush()
            if not ok:
                print(f"error: did not see initial data-entry address for chunk at {chunk_base:04X}", file=sys.stderr)
                return False

            for i, b in enumerate(chunk):
                this_addr = chunk_base + i
                next_addr = this_addr + 1

                self.send_line(f"{b:02X}")

                if i == len(chunk) - 1:
                    sent_total += 1
                    if verbose and report_every > 0 and sent_total >= next_report:
                        print(f"  sent {sent_total}/{len(data)}")
                        next_report += report_every
                    break

                ok_next, text, highest_seen = self.wait_for_address_progress(
                    expected_addr=next_addr,
                    floor_addr=this_addr,
                    timeout=5.0,
                )

                if text:
                    sys.stdout.write(text)
                    sys.stdout.flush()

                if not ok_next:
                    hs = "none" if highest_seen is None else f"{highest_seen:04X}"
                    print(f"\nerror: monitor did not advance far enough after {this_addr:04X}", file=sys.stderr)
                    print(f"expected at least: {next_addr:04X}", file=sys.stderr)
                    print(f"highest address seen: {hs}", file=sys.stderr)
                    print(f"last attempted byte: {b:02X}", file=sys.stderr)
                    return False

                sent_total += 1
                if verbose and report_every > 0 and sent_total >= next_report:
                    print(f"  sent {sent_total}/{len(data)}")
                    next_report += report_every

            ok_close, close_text = self.close_monitor_data_mode(timeout=6.0)
            if close_text:
                sys.stdout.write(close_text)
                sys.stdout.flush()
            if not ok_close:
                print(f"error: did not get clean monitor prompt after chunk ending at {chunk_end:04X}", file=sys.stderr)
                return False

            hb_ok2, hb_text2 = self.require_monitor_heartbeat()
            if hb_text2:
                sys.stdout.write(hb_text2)
                sys.stdout.flush()
            if not hb_ok2:
                print(f"error: monitor heartbeat failed after chunk ending at {chunk_end:04X}", file=sys.stderr)
                return False

        if verbose:
            print(f"\nUpload complete: {sent_total} bytes")

        return True

    def verify_memory_against_file(
        self,
        bin_path: str,
        base: int,
        sample_len: int = 64,
    ) -> None:
        data = Path(bin_path).read_bytes()
        n = min(sample_len, len(data))
        print(f"Sampling first {n} bytes at {base:04X}h")
        out = self.send_command_and_read(f"d {base:04X} {n:X}", settle=0.60, max_total=4.0)
        if out:
            sys.stdout.write(out)
            sys.stdout.flush()

    # ------------------------------------------------------------------
    # Run flow
    # ------------------------------------------------------------------

    def run_binary_flow(
        self,
        bin_path: str,
        base: int = 0x6000,
        chunk_size: int = 0x400,
        prefill_start: Optional[int] = None,
        prefill_end: Optional[int] = None,
        prefill_value: int = 0x00,
        prefill_count: int = 0,
        verify_after_load: bool = True,
        verify_len: int = 0x40,
        jump_after_load: bool = True,
    ) -> bool:
        hb_ok, hb_text = self.require_monitor_heartbeat()
        if hb_text:
            sys.stdout.write(hb_text)
            sys.stdout.flush()
        if not hb_ok:
            print("error: no clean initial monitor heartbeat", file=sys.stderr)
            return False

        if prefill_count > 0:
            if prefill_start is None or prefill_end is None:
                print("error: prefill_count > 0 but prefill range not specified", file=sys.stderr)
                return False

            print(
                f"Prefill workaround: {prefill_count} pass(es) of "
                f"f {prefill_start:04X} {prefill_end:04X} {prefill_value:02X}"
            )
            ok_prefill = self.prefill_memory_range(
                start_addr=prefill_start,
                end_addr=prefill_end,
                value=prefill_value,
                passes=prefill_count,
            )
            if not ok_prefill:
                return False

        ok_load = self.load_binary_via_monitor_chunked(
            bin_path=bin_path,
            base=base,
            chunk_size=chunk_size,
        )
        if not ok_load:
            return False

        if verify_after_load:
            self.verify_memory_against_file(bin_path, base, verify_len)

        if jump_after_load:
            ok_jump, out = self.jump_and_expect_companion(base)
            if out:
                sys.stdout.write(out)
                sys.stdout.flush()
            if not ok_jump:
                print("warning: jump did not clearly land in companion mode", file=sys.stderr)
            return ok_jump

        return True

    # ------------------------------------------------------------------
    # Jump helpers
    # ------------------------------------------------------------------

    def jump(self, addr: int, wait_time: float = 3.0) -> str:
        self.send_line(f"j {addr:04X}")
        out = self.read_until_quiet(quiet_for=0.50, max_total=wait_time)
        self._update_mode_from_text(out)
        return out

    def jump_and_expect_companion(self, addr: int, timeout: float = 10.0) -> tuple[bool, str]:
        self.send_line(f"j {addr:04X}")
        ok, text = self.wait_for_companion_prompt(timeout=timeout, quiet_for=1.5)
        if ok:
            self.current_mode = "companion"
        else:
            self._update_mode_from_text(text)
        return ok, text

    # ------------------------------------------------------------------
    # Companion upload helpers
    # ------------------------------------------------------------------

    def _load_ihex_lines(self, in_path: str) -> list[str]:
        lines = Path(in_path).read_text(encoding="ascii", errors="strict").splitlines()
        ihex_lines = [ln.strip() for ln in lines if ln.strip()]
        if not ihex_lines:
            raise ValueError(f"{in_path} contained no Intel-HEX lines")
        return ihex_lines

    def _watch_companion_after_line(
        self,
        dwell: float = 0.08,
        max_total: float = 0.25,
    ) -> SendWatchResult:
        text = self.poll_output_once(dwell=dwell, max_total=max_total)
        if text:
            sys.stdout.write(text)
            sys.stdout.flush()

        if not text:
            return SendWatchResult(False, False, None, text)

        if COMPANION_PROMPT_RE.search(text):
            return SendWatchResult(True, True, "prompt", text)

        for pat in COMPANION_ERROR_PATTERNS:
            m = pat.search(text)
            if m:
                return SendWatchResult(True, False, m.group(0), text)

        return SendWatchResult(False, False, None, text)

    def _send_ihex_lines_with_watch(
        self,
        ihex_lines: list[str],
        report_every_lines: int = 8,
        prefix: str = "send",
        watch_dwell: float = 0.08,
        watch_max_total: float = 0.25,
    ) -> SendWatchResult:
        total_lines = len(ihex_lines)
        total_chars = sum(len(ln) + 1 for ln in ihex_lines)  # +CR
        print(f"{prefix}: {total_lines} Intel-HEX lines, about {total_chars} chars")

        combined_transcript: list[str] = []
        for idx, ln in enumerate(ihex_lines, start=1):
            self.send_line(ln)

            if report_every_lines > 0 and (idx == 1 or idx % report_every_lines == 0 or idx == total_lines):
                print(f"  {prefix} line {idx}/{total_lines}")

            watch = self._watch_companion_after_line(
                dwell=watch_dwell,
                max_total=watch_max_total,
            )
            if watch.transcript:
                combined_transcript.append(watch.transcript)

            if watch.aborted:
                why = watch.reason or "unknown"
                print(f"{prefix}: stopped early after line {idx}/{total_lines} ({why})")
                return SendWatchResult(True, watch.saw_prompt, why, "".join(combined_transcript))

        return SendWatchResult(False, False, None, "".join(combined_transcript))

    # ------------------------------------------------------------------
    # Companion track helpers
    # ------------------------------------------------------------------

    def yank_track_to_file(
        self,
        track_hex: str,
        out_path: str,
        timeout: float = 30.0,
        quiet_for: float = 1.5,
    ) -> bool:
        track_hex = track_hex.upper()
        if len(track_hex) != 2 or any(c not in "0123456789ABCDEF" for c in track_hex):
            print("error: track must be exactly two hex digits, e.g. 17", file=sys.stderr)
            return False

        ok, text = self.send_companion_command(f"Y{track_hex}", timeout=timeout, quiet_for=quiet_for)
        if text:
            sys.stdout.write(text)
            sys.stdout.flush()

        ihex_lines = extract_ihex_lines(text)
        print(f"[debug] extracted {len(ihex_lines)} Intel-HEX lines")

        if not ihex_lines:
            print("error: no Intel HEX lines captured from Y command", file=sys.stderr)
            return False

        payload = "\n".join(ihex_lines) + "\n"
        Path(out_path).write_text(payload, encoding="ascii")
        print(f"Wrote {len(ihex_lines)} Intel-HEX lines to {out_path}")
        return ok

    def take_track_from_file(
        self,
        track_hex: str,
        in_path: str,
        start_wait: float = 0.75,
        timeout: float = 40.0,
        quiet_for: float = 1.5,
        report_every_lines: int = 8,
    ) -> bool:
        track_hex = track_hex.upper()
        if len(track_hex) != 2 or any(c not in "0123456789ABCDEF" for c in track_hex):
            print("error: track must be exactly two hex digits, e.g. 17", file=sys.stderr)
            return False

        try:
            ihex_lines = self._load_ihex_lines(in_path)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return False

        self.current_mode = "companion"
        _ = self.send_safe_nop_probe()

        self.send_line(f"T{track_hex}")
        time.sleep(start_wait)

        watch = self._send_ihex_lines_with_watch(
            ihex_lines,
            report_every_lines=report_every_lines,
            prefix="take",
        )

        if watch.aborted:
            return False

        ok, text = self.wait_for_companion_prompt(timeout=timeout, quiet_for=quiet_for)
        if text:
            sys.stdout.write(text)
            sys.stdout.flush()
        return ok

    def diag_track_roundtrip_to_file(
        self,
        track_hex: str,
        in_path: str,
        out_path: str,
        start_wait: float = 0.75,
        timeout: float = 40.0,
        quiet_for: float = 1.5,
        report_every_lines: int = 8,
    ) -> bool:
        track_hex = track_hex.upper()
        if len(track_hex) != 2 or any(c not in "0123456789ABCDEF" for c in track_hex):
            print("error: track must be exactly two hex digits, e.g. 17", file=sys.stderr)
            return False

        try:
            ihex_lines = self._load_ihex_lines(in_path)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return False

        self.current_mode = "companion"
        _ = self.send_safe_nop_probe()

        self.send_line(f"U{track_hex}")
        time.sleep(start_wait)

        watch = self._send_ihex_lines_with_watch(
            ihex_lines,
            report_every_lines=report_every_lines,
            prefix="diag",
        )

        if watch.aborted:
            return False

        ok, text = self.wait_for_companion_prompt(timeout=timeout, quiet_for=quiet_for)
        if text:
            sys.stdout.write(text)
            sys.stdout.flush()

        ihex_out = extract_ihex_lines(text)
        print(f"[debug] extracted {len(ihex_out)} Intel-HEX lines from U command")

        if not ihex_out:
            print("error: no Intel HEX lines captured from U command", file=sys.stderr)
            return False

        payload = "\n".join(ihex_out) + "\n"
        Path(out_path).write_text(payload, encoding="ascii")
        print(f"Wrote {len(ihex_out)} Intel-HEX lines to {out_path}")
        return ok

    def verify_track_writeback_to_file(
        self,
        track_hex: str,
        in_path: str,
        out_path: str,
        start_wait: float = 0.75,
        timeout: float = 60.0,
        quiet_for: float = 1.5,
        report_every_lines: int = 8,
    ) -> bool:
        """
        Vxx path:
        - send Vxx
        - stream one Intel-HEX track file to target
        - target prints progress/debug during receive
        - target low-level writes track, then PROM-reads it back
        - target dumps readback as Intel-HEX
        - save returned dump to out_path
        """
        track_hex = track_hex.upper()
        if len(track_hex) != 2 or any(c not in "0123456789ABCDEF" for c in track_hex):
            print("error: track must be exactly two hex digits, e.g. 17", file=sys.stderr)
            return False

        try:
            ihex_lines = self._load_ihex_lines(in_path)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return False

        self.current_mode = "companion"
        _ = self.send_safe_nop_probe()

        self.send_line(f"V{track_hex}")
        time.sleep(start_wait)

        watch = self._send_ihex_lines_with_watch(
            ihex_lines,
            report_every_lines=report_every_lines,
            prefix="verify",
        )

        if watch.aborted:
            return False

        ok, text = self.wait_for_companion_prompt(timeout=timeout, quiet_for=quiet_for)
        if text:
            sys.stdout.write(text)
            sys.stdout.flush()

        ihex_out = extract_ihex_lines(text)
        print(f"[debug] extracted {len(ihex_out)} Intel-HEX lines from V command")

        if not ihex_out:
            print("error: no Intel HEX lines captured from V command", file=sys.stderr)
            return False

        payload = "\n".join(ihex_out) + "\n"
        Path(out_path).write_text(payload, encoding="ascii")
        print(f"Wrote {len(ihex_out)} Intel-HEX lines to {out_path}")
        return ok

    # ------------------------------------------------------------------
    # Interactive shell
    # ------------------------------------------------------------------

    def interactive_shell(self) -> int:
        print("\nInteractive mode.")
        print("Local commands:")
        print("  /quit")
        print("  /break")
        print("  /empty")
        print("  /sync")
        print("  /mode monitor|companion")
        print("  /pace <ms>")
        print("  /prepfill <starthex> <endhex> <valhex> [count]")
        print("  /load <bin> [basehex] [chunksizehex]")
        print("  /run <bin> [basehex] [chunksizehex]")
        print("  /jump <addrhex>")
        print("  /verify <bin> [basehex] [samplelenhex]")
        print("  /comp <text>")
        print("  /yank <trackhex> <outfile>")
        print("  /take <trackhex> <infile>")
        print("  /diag <trackhex> <infile> <outfile>")
        print("  /vfy <trackhex> <infile> <outfile>")
        print(f"\nCurrent mode: {self.current_mode}")
        print(f"Current TX pacing: {self.tx_char_delay*1000:.1f} ms/char\n")

        printer_stop = threading.Event()

        def printer() -> None:
            while not printer_stop.is_set():
                try:
                    chunk = self._rx_queue.get(timeout=0.10)
                except queue.Empty:
                    continue
                sys.stdout.write(chunk)
                sys.stdout.flush()
                self._update_mode_from_text(chunk)

        t = threading.Thread(target=printer, daemon=True)
        t.start()

        try:
            while True:
                try:
                    line = input()
                except EOFError:
                    print()
                    break
                except KeyboardInterrupt:
                    print()
                    continue

                line = line.strip()
                if not line:
                    continue

                if line == "/quit":
                    break
                elif line == "/break":
                    self.write_raw_paced(b"\x03")
                elif line == "/empty":
                    self.write_raw_paced(b"\r")
                elif line == "/sync":
                    res = self.sync_auto()
                    if res.transcript:
                        sys.stdout.write(res.transcript)
                        sys.stdout.flush()
                    print(f"\n[sync success={res.success} mode={res.mode}]")
                elif line.startswith("/mode "):
                    parts = line.split()
                    if len(parts) != 2 or parts[1] not in ("monitor", "companion"):
                        print("usage: /mode monitor|companion")
                        continue
                    self.current_mode = parts[1]
                    print(f"[mode set to {self.current_mode}]")
                elif line.startswith("/pace "):
                    parts = line.split()
                    if len(parts) != 2:
                        print("usage: /pace <ms>")
                        continue
                    ms = float(parts[1])
                    self.tx_char_delay = ms / 1000.0
                    print(f"[tx pacing set to {ms:.1f} ms/char]")
                elif line.startswith("/prepfill "):
                    parts = line.split()
                    if len(parts) not in (4, 5):
                        print("usage: /prepfill <starthex> <endhex> <valhex> [count]")
                        continue
                    start = int(parts[1], 16)
                    end = int(parts[2], 16)
                    val = int(parts[3], 16)
                    count = int(parts[4], 0) if len(parts) == 5 else 2
                    self.prefill_memory_range(start, end, val, passes=count)
                elif line.startswith("/jump "):
                    parts = line.split()
                    if len(parts) != 2:
                        print("usage: /jump <addrhex>")
                        continue
                    addr = int(parts[1], 16)
                    ok, out = self.jump_and_expect_companion(addr)
                    if out:
                        sys.stdout.write(out)
                        sys.stdout.flush()
                    print(f"\n[jump companion_detected={ok} mode={self.current_mode}]")
                elif line.startswith("/run "):
                    parts = line.split()
                    if len(parts) not in (2, 3, 4):
                        print("usage: /run <bin> [basehex] [chunksizehex]")
                        continue
                    path = parts[1]
                    base = int(parts[2], 16) if len(parts) >= 3 else 0x6000
                    chunk_size = int(parts[3], 16) if len(parts) >= 4 else 0x400
                    ok = self.run_binary_flow(
                        bin_path=path,
                        base=base,
                        chunk_size=chunk_size,
                        prefill_start=0x6000,
                        prefill_end=0x7FFF,
                        prefill_value=0xFF,
                        prefill_count=2,
                        verify_after_load=True,
                        verify_len=0x40,
                        jump_after_load=True,
                    )
                    print(f"\n[run success={ok} mode={self.current_mode}]")
                elif line.startswith("/load "):
                    parts = line.split()
                    if len(parts) not in (2, 3, 4):
                        print("usage: /load <bin> [basehex] [chunksizehex]")
                        continue
                    path = parts[1]
                    base = int(parts[2], 16) if len(parts) >= 3 else 0x6000
                    chunk_size = int(parts[3], 16) if len(parts) >= 4 else 0x400
                    self.load_binary_via_monitor_chunked(path, base, chunk_size=chunk_size)
                elif line.startswith("/verify "):
                    parts = line.split()
                    if len(parts) not in (2, 3, 4):
                        print("usage: /verify <bin> [basehex] [samplelenhex]")
                        continue
                    path = parts[1]
                    base = int(parts[2], 16) if len(parts) >= 3 else 0x6000
                    sample_len = int(parts[3], 16) if len(parts) >= 4 else 0x40
                    self.verify_memory_against_file(path, base, sample_len)
                elif line.startswith("/comp "):
                    cmd = line[len("/comp "):]
                    ok, out = self.send_companion_command(cmd)
                    if out:
                        sys.stdout.write(out)
                        sys.stdout.flush()
                    print(f"\n[companion command success={ok} mode={self.current_mode}]")
                elif line.startswith("/yank "):
                    parts = line.split()
                    if len(parts) != 3:
                        print("usage: /yank <trackhex> <outfile>")
                        continue
                    ok = self.yank_track_to_file(parts[1], parts[2])
                    print(f"\n[yank success={ok} mode={self.current_mode}]")
                elif line.startswith("/take "):
                    parts = line.split()
                    if len(parts) != 3:
                        print("usage: /take <trackhex> <infile>")
                        continue
                    ok = self.take_track_from_file(parts[1], parts[2])
                    print(f"\n[take success={ok} mode={self.current_mode}]")
                elif line.startswith("/diag "):
                    parts = line.split()
                    if len(parts) != 4:
                        print("usage: /diag <trackhex> <infile> <outfile>")
                        continue
                    ok = self.diag_track_roundtrip_to_file(parts[1], parts[2], parts[3])
                    print(f"\n[diag success={ok} mode={self.current_mode}]")
                elif line.startswith("/vfy "):
                    parts = line.split()
                    if len(parts) != 4:
                        print("usage: /vfy <trackhex> <infile> <outfile>")
                        continue
                    ok = self.verify_track_writeback_to_file(parts[1], parts[2], parts[3])
                    print(f"\n[verify-track success={ok} mode={self.current_mode}]")
                else:
                    if self.current_mode == "companion":
                        ok, out = self.send_companion_command(line)
                        if out:
                            sys.stdout.write(out)
                            sys.stdout.flush()
                        print(f"\n[companion command success={ok} mode={self.current_mode}]")
                    else:
                        self.send_line(line)

        finally:
            printer_stop.set()
            t.join(timeout=0.5)

        return 0

    # ------------------------------------------------------------------
    # MCZ disk image writer
    # ------------------------------------------------------------------

    def write_disk_from_mcz(
        self,
        image_path: str,
        ready_timeout: float = 15.0,
        track_timeout: float = 60.0,
    ) -> bool:
        """
        Write a full MCZ disk image to the companion using the X D command.

        Image format: 77 tracks x 32 sectors x 136 bytes = 315,392 bytes
        Sector layout: SA(1) TA(1) DATA(128) BCK(2) FWD(2) CRC(2)

        P-format hex line per sector:
            : SA TA D0..D127 L0 L1 L2 L3 CR LF
        (134 space-separated hex pairs prefixed by colon)

        The companion's RXSC0 has a ~10s timeout waiting for ':'.
        tx_line_delay (default 200ms) paces after each LF.
        """
        TRACKS = 77
        SECTORS = 32
        SECTOR_SIZE = 136
        EXPECTED = TRACKS * SECTORS * SECTOR_SIZE  # 315392

        path = Path(image_path)
        if not path.exists():
            print(f"error: image file not found: {image_path}", file=sys.stderr)
            return False

        data = path.read_bytes()
        if len(data) != EXPECTED:
            print(
                f"error: image size {len(data)} != {EXPECTED} "
                f"(expected {TRACKS}x{SECTORS}x{SECTOR_SIZE})",
                file=sys.stderr,
            )
            return False

        print(f"[xd] image: {image_path} ({len(data)} bytes)")
        print(f"[xd] pacing: {self.tx_char_delay*1000:.0f}ms/char  "
              f"{self.tx_line_delay*1000:.0f}ms/line")
        print(f"[xd] sending XD command to companion...")

        # Drain any stale output (e.g. prompt already sitting there)
        self.drain_output(quiet_for=0.5, max_total=2.0)

        # Send X D command
        self.send_line("xd")
        time.sleep(0.5)

        # Wait for '>' ready signal
        deadline = time.monotonic() + ready_timeout
        buf = ""
        while time.monotonic() < deadline:
            chunk = ""
            try:
                chunk = self._rx_queue.get(timeout=0.1)
            except Exception:
                pass
            if chunk:
                buf += chunk
                sys.stdout.write(chunk)
                sys.stdout.flush()
            if ">" in buf:
                break
        else:
            print("\nerror: timed out waiting for '>' ready signal", file=sys.stderr)
            return False

        print(f"\n[xd] got ready signal, starting transfer...")

        # Stream all sectors, waiting for '>' before each track
        offset = 0
        for track in range(TRACKS):
            # Wait for '>' ready signal before sending this track's data
            # (first track: already got '>' above; subsequent tracks: companion sends '>' after write+step)
            if track > 0:
                deadline = time.monotonic() + ready_timeout
                buf = ""
                while time.monotonic() < deadline:
                    chunk = ""
                    try:
                        chunk = self._rx_queue.get(timeout=0.1)
                    except Exception:
                        pass
                    if chunk:
                        buf += chunk
                        sys.stdout.write(chunk)
                        sys.stdout.flush()
                    if ">" in buf:
                        break
                else:
                    print(f"\nerror: timed out waiting for '>' before track {track:02X}", file=sys.stderr)
                    return False

            for sec in range(SECTORS):
                sector = data[offset:offset + SECTOR_SIZE]
                offset += SECTOR_SIZE

                sa   = sector[0]
                ta   = sector[1]
                body = sector[2:SECTOR_SIZE]

                parts = [f"{sa:02X}", f"{ta:02X}"]
                parts += [f"{b:02X}" for b in body[:132]]
                line = ": " + " ".join(parts) + "\r\n"

                self.write_raw_paced(line.encode("ascii"))

            # Drain companion output after sending track (TXX progress)
            try:
                while True:
                    chunk = self._rx_queue.get_nowait()
                    sys.stdout.write(chunk)
                    sys.stdout.flush()
            except Exception:
                pass

            sys.stdout.write(f"[{track:02X}] ")
            sys.stdout.flush()

        print(f"\n[xd] all sectors sent, waiting for DONE...")

        # Wait for DONE
        ok, text = self.wait_for_companion_prompt(timeout=track_timeout, quiet_for=2.0)
        if text:
            sys.stdout.write(text)
            sys.stdout.flush()

        if ok:
            print("[xd] disk write complete!")
        else:
            print("[xd] warning: did not see clean companion prompt after transfer", file=sys.stderr)

        return ok


def build_argparser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Serial client for BBLMCZ/Zilog PROM monitor and MCZIMAGER companion"
    )
    ap.add_argument("port", help="Serial device, e.g. /dev/ttyUSB0")
    ap.add_argument("--baud", type=int, default=9600, help="Baud rate (default: 9600)")
    ap.add_argument("--log", default=None, help="Optional transcript log file")
    ap.add_argument("--no-shell", action="store_true", help="Do not enter interactive mode after sync")

    ap.add_argument("--tx-char-ms", type=float, default=20.0, help="Transmit pacing in milliseconds per character (default: 20)")
    ap.add_argument("--tx-line-ms", type=float, default=0.0, help="Extra delay in milliseconds after CR/LF (default: 0)")
    ap.add_argument("--mode", choices=["monitor", "companion"], default="monitor", help="Initial session mode assumption (default: monitor)")
    ap.add_argument("--comp-report-lines", type=int, default=8, help="Progress report interval while sending Intel-HEX lines for T/U/V (default: 8)")

    ap.add_argument("--load-bin", default=None, help="Upload BIN file through monitor d/q entry")
    ap.add_argument("--run-bin", default=None, help="Full flow: optional prefill + load + verify + jump")
    ap.add_argument("--base", default="6000", help="Hex base address for load/jump (default: 6000)")
    ap.add_argument("--chunk-size", default="400", help="Chunk size in hex bytes for monitor load sessions (default: 400)")
    ap.add_argument("--jump-after-load", action="store_true", help="After load, issue j BASE and wait for companion banner/prompt")
    ap.add_argument("--once", default=None, help="Send one PROM command after sync, print response, then exit")
    ap.add_argument("--comp-once", default=None, help="Send one companion command after sync/run and print response, then exit")
    ap.add_argument("--verify-after-load", action="store_true", help="After load, sample memory with d BASE 40")

    ap.add_argument("--yank-track", default=None, help="Run Yxx in companion mode, e.g. 17")
    ap.add_argument("--yank-out", default=None, help="Output file for --yank-track")
    ap.add_argument("--take-track", default=None, help="Run Txx in companion mode, e.g. 17")
    ap.add_argument("--take-in", default=None, help="Input Intel-HEX file for --take-track")
    ap.add_argument("--diag-track", default=None, help="Run Uxx in companion mode, e.g. 17")
    ap.add_argument("--diag-in", default=None, help="Input Intel-HEX file for --diag-track")
    ap.add_argument("--diag-out", default=None, help="Output Intel-HEX file for --diag-track")
    ap.add_argument("--verify-track", default=None, help="Run Vxx in companion mode, e.g. 17")
    ap.add_argument("--verify-in", default=None, help="Input Intel-HEX file for --verify-track")
    ap.add_argument("--verify-out", default=None, help="Output Intel-HEX file for --verify-track")

    ap.add_argument("--prefill-start", default=None, help="Optional prefill start address, hex")
    ap.add_argument("--prefill-end", default=None, help="Optional prefill end address, hex")
    ap.add_argument("--prefill-value", default="00", help="Prefill byte value, hex (default: 00)")
    ap.add_argument("--prefill-count", type=int, default=0, help="Number of prefill passes before load (default: 0)")
    ap.add_argument("--verify-len", default="40", help="Verify sample length in hex (default: 40)")

    ap.add_argument("--write-disk", default=None, help="Write MCZ disk image via X D command, e.g. rio22.mcz")

    return ap


def main() -> int:
    args = build_argparser().parse_args()

    client = SerialMonitorClient(
        port=args.port,
        baudrate=args.baud,
        log_path=args.log,
        tx_char_delay=args.tx_char_ms / 1000.0,
        tx_line_delay=args.tx_line_ms / 1000.0,
    )
    client.current_mode = args.mode

    try:
        client.open()
    except Exception as e:
        print(f"error opening {args.port}: {e}", file=sys.stderr)
        return 1

    try:
        sync = client.sync_auto()
        if sync.transcript:
            sys.stdout.write(sync.transcript)
            sys.stdout.flush()

        if not sync.success:
            print("\nDid not get a recognizable response during initial sync.", file=sys.stderr)
            return 2

        base = int(args.base, 16)
        chunk_size = int(args.chunk_size, 16)
        verify_len = int(args.verify_len, 16)

        prefill_start = int(args.prefill_start, 16) if args.prefill_start is not None else None
        prefill_end = int(args.prefill_end, 16) if args.prefill_end is not None else None
        prefill_value = int(args.prefill_value, 16)

        if args.once is not None:
            out = client.send_command_and_read(args.once)
            sys.stdout.write(out)
            sys.stdout.flush()
            return 0

        if args.run_bin is not None:
            ok = client.run_binary_flow(
                bin_path=args.run_bin,
                base=base,
                chunk_size=chunk_size,
                prefill_start=prefill_start,
                prefill_end=prefill_end,
                prefill_value=prefill_value,
                prefill_count=args.prefill_count,
                verify_after_load=True,
                verify_len=verify_len,
                jump_after_load=True,
            )
            if not ok:
                return 3

            if args.comp_once is not None:
                okc, out = client.send_companion_command(args.comp_once)
                if out:
                    sys.stdout.write(out)
                    sys.stdout.flush()
                return 0 if okc else 4

        elif args.load_bin is not None:
            if args.prefill_count > 0:
                if prefill_start is None or prefill_end is None:
                    print("error: prefill_count requested but prefill range not provided", file=sys.stderr)
                    return 3
                ok_prefill = client.prefill_memory_range(
                    prefill_start,
                    prefill_end,
                    prefill_value,
                    passes=args.prefill_count,
                )
                if not ok_prefill:
                    return 3

            ok = client.load_binary_via_monitor_chunked(
                args.load_bin,
                base=base,
                chunk_size=chunk_size,
            )
            if not ok:
                return 3

            if args.verify_after_load:
                client.verify_memory_against_file(args.load_bin, base=base, sample_len=verify_len)

            if args.jump_after_load:
                ok2, out = client.jump_and_expect_companion(base)
                if out:
                    sys.stdout.write(out)
                    sys.stdout.flush()
                if not ok2:
                    print("warning: jump did not clearly land in companion mode", file=sys.stderr)

            if args.comp_once is not None:
                okc, out = client.send_companion_command(args.comp_once)
                if out:
                    sys.stdout.write(out)
                    sys.stdout.flush()
                return 0 if okc else 4

        if args.yank_track is not None:
            if not args.yank_out:
                print("error: --yank-track requires --yank-out", file=sys.stderr)
                return 5
            ok = client.yank_track_to_file(args.yank_track, args.yank_out)
            return 0 if ok else 6

        if args.take_track is not None:
            if not args.take_in:
                print("error: --take-track requires --take-in", file=sys.stderr)
                return 5
            ok = client.take_track_from_file(
                args.take_track,
                args.take_in,
                report_every_lines=args.comp_report_lines,
            )
            return 0 if ok else 7

        if args.diag_track is not None:
            if not args.diag_in or not args.diag_out:
                print("error: --diag-track requires --diag-in and --diag-out", file=sys.stderr)
                return 5
            ok = client.diag_track_roundtrip_to_file(
                args.diag_track,
                args.diag_in,
                args.diag_out,
                report_every_lines=args.comp_report_lines,
            )
            return 0 if ok else 8

        if args.verify_track is not None:
            if not args.verify_in or not args.verify_out:
                print("error: --verify-track requires --verify-in and --verify-out", file=sys.stderr)
                return 5
            ok = client.verify_track_writeback_to_file(
                args.verify_track,
                args.verify_in,
                args.verify_out,
                report_every_lines=args.comp_report_lines,
            )
            return 0 if ok else 9

        if args.comp_once is not None:
            okc, out = client.send_companion_command(args.comp_once)
            if out:
                sys.stdout.write(out)
                sys.stdout.flush()
            return 0 if okc else 4

        if args.write_disk is not None:
            ok = client.write_disk_from_mcz(args.write_disk)
            return 0 if ok else 5

        if args.no_shell:
            return 0

        return client.interactive_shell()

    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
