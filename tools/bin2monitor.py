#!/usr/bin/env python3
from pathlib import Path
import argparse

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("bin", help="input BIN file")
    ap.add_argument("--base", default="6000", help="load address (hex)")
    ap.add_argument("-o", "--out", default="-", help="output file (default stdout)")
    args = ap.parse_args()

    base = int(args.base, 16)
    data = Path(args.bin).read_bytes()

    lines = []
    lines.append(f"d {base:04X}")

    for b in data:
        lines.append(f"{b:02X}")

    lines.append("q")

    output = "\n".join(lines) + "\n"

    if args.out == "-":
        print(output, end="")
    else:
        Path(args.out).write_text(output)

if __name__ == "__main__":
    main()
