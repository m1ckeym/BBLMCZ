# MCZIMAGER Port — Summary of Changes

## Original: MCZIMAGER.S by Les Bird (2021)
## Ported:   MCZIMAGER_NEW.S for new board

---

## Hardware Changes

| Resource | Original MCZ | New Board |
|----------|-------------|-----------|
| Serial data port | 0DEH | 092H |
| Serial control port | 0DFH | 093H |
| RX ready bit | Bit 1 | Bit 0 |
| TX ready bit | Bit 0 | Bit 2 |
| Bit polarity | Active high (JR Z to wait) | Same — active high |
| Disk data (DSKDAT) | 0CFH | 0CFH (unchanged) |
| Disk control (DSKCOM/DSSTAT) | 0D0H | 0D0H (unchanged) |
| Disk select (DSKSEL) | 0D1H | 0D1H (unchanged) |
| CTC (CLK0) | 0D4H | 0D4H (unchanged) |
| Drive 0 select bit | Bit 3 (08H) | Bit 0 (01H) |
| PROM base address | 0800H | 0000H |
| FLOPPY entry | 0BFDH | 0BFDH (same offset) |

## Bugs Fixed During Porting

### 1. Missing ORG directive
**Symptom:** Code crashed immediately on `J 6000` — monitor showed `?>`.
**Cause:** No `ORG 6000H` meant all absolute addresses (stack, variables, data buffer) resolved to low memory overlapping the PROM at 0000H–0FFFH. Writing to ROM silently failed; return addresses were garbage.
**Fix:** Added `ORG 6000H` at top of source.

### 2. Drive select bit wrong
**Symptom:** G command hung after seeking (no sector pulses detected).
**Cause:** Original MCZ used bit 3 for drive 0 (`SET 3,A` = 08H). New board's PROM lookup table at 0B1BH shows drive 0 = 01H (bit 0).
**Fix:** Changed SELDSK to `SET 0,A` and DESDSK to `BIT 0,A` / `AND 06H`.

### 3. Input character case conversion
**Symptom:** All keypresses treated as NOP — menu re-prompted endlessly.
**Cause:** The PROM's monitor uses `RES 5,A` (force uppercase) before every command comparison. Our TTYIN returned raw characters; lowercase 'q' (71H) never matched 'Q' (51H).
**Fix:** Added `AND 7FH` (strip bit 7) and `RES 5,A` (force uppercase) in the command input path at BEGIN. Not added inside TTYIN itself to preserve 8-bit binary data for disk read/write paths.

### 4. CTC interrupt race condition
**Symptom:** G command hung at sector wait loop (RDTRK1) — CTC interrupt never set SECFLG.
**Cause:** Original code armed the CTC (time constant = 1) BEFORE setting INTPNT to the handler. With TC=1, the very next sector pulse fires immediately. If the interrupt occurs before INTPNT is written, the handler dispatches through stale INTPNT (wrong address), SECFLG never gets set.
**Fix:** Reordered all three routines (RDTRK0, WAIDX1, WASEC) to set INTPNT before programming the CTC.

### 5. Startup interrupt crash (red herring, then real fix)
**Symptom:** First version crashed on EI at startup.
**Root cause:** This was actually caused by the missing ORG (bug #1), not by EI itself. Once ORG 6000H was added, EI at startup works correctly.
**Final state:** Startup does `LD A,13H` / `LD I,A` / `IM 2` / `EI` — required because the FLOPPY routine expects interrupts enabled.

## New Features Added

### G command — Intel HEX output
Changed from raw binary to Intel HEX format for 7-bit TTY compatibility. Outputs 16-byte data records with sequential addresses across all 77 tracks, terminated by EOF record.

### F command — Dummy payload mode
Replaced TTY input with FILDAT routine that fills each track buffer with a deterministic test pattern (sector number as seed, incrementing bytes). Used for testing disk writes without a host.

### D command — Dump single sector
Reads track 23, searches buffer for sector 3, outputs the 136-byte sector record as Intel HEX.

### B command — Burn single sector
Reads Intel HEX from TTY into DATA buffer, forces sector=3/track=23, writes via CALL FLOPPY.

### O command — Overwrite full disk from hex
Reads Intel HEX from TTY, buffers one track (1100H bytes) at a time, writes with WRTRK1 (direct write), prints track number as progress. Accepts the exact output format of G.

### FLUSH routine
Drains TTY receive buffer after B/O commands complete or on error, preventing leftover paste data from triggering menu commands.

### RXHEXB / RXHNIB / ADDHXS routines
Intel HEX input parsing: reads two hex ASCII chars as a byte, with checksum accumulation. Handles both upper and lowercase hex digits.

### PRHEX2 routine
Prints a byte as 2 hex ASCII digits (used for O command progress display).

## Command Reference

| Key | Function | I/O Format |
|-----|----------|------------|
| Q | Quit to monitor | — |
| G | Read full disk (direct) | Intel HEX output |
| R | Read full disk (PROM) | Raw binary output |
| F | Write full disk (direct, dummy data) | No input needed |
| W | Write full disk (PROM) | Raw binary input |
| D | Dump track 23 sector 3 | Intel HEX output |
| B | Burn track 23 sector 3 | Intel HEX input |
| O | Overwrite full disk | Intel HEX input |

## Memory Map (with ORG 6000H)

- 0000H–0FFFH: PROM (boot/monitor/RIO-compatible)
- 1000H–17FFH: PROM RAM (vectors, variables, DSKVSL, INTPNT, PTRS)
- 6000H–~6500H: MCZIMAGER code + variables + messages
- ~6500H–~6580H: Stack (80H bytes)
- ~6580H–~7680H: DATA buffer (1100H bytes = 1 track)
