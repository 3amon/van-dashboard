#!/usr/bin/env python3
"""Generate a fully-transparent XCursor theme named 'blank'.

labwc has no "hide cursor" option, but it honours XCURSOR_THEME. Pointing it at
a transparent theme hides the compositor cursor at all times — including the
boot moment before any touch (when a CSS `cursor:none` hasn't taken effect yet).

Usage: make-blank-cursor.py [theme_dir]   (default ~/.local/share/icons/blank)
"""
import os
import struct
import sys

SIZE = 24
NAMES = ["default", "left_ptr", "arrow", "top_left_arrow", "x-cursor",
         "hand1", "hand2", "pointer", "text", "xterm"]


def blank_cursor(size=SIZE):
    w = h = size
    pixels = b"\x00\x00\x00\x00" * (w * h)          # fully transparent ARGB
    # image chunk: 9-uint32 header (len=36) then pixels
    chunk = struct.pack("<9I", 36, 0xfffd0002, size, 1, w, h, 0, 0, 0) + pixels
    ntoc = 1
    header = b"Xcur" + struct.pack("<3I", 16, 0x00010000, ntoc)  # 16 bytes
    chunk_off = len(header) + 12 * ntoc                          # = 28
    toc = struct.pack("<3I", 0xfffd0002, size, chunk_off)
    return header + toc + chunk


def main():
    dest = os.path.expanduser(sys.argv[1] if len(sys.argv) > 1
                              else "~/.local/share/icons/blank")
    cursors = os.path.join(dest, "cursors")
    os.makedirs(cursors, exist_ok=True)
    data = blank_cursor()
    first = os.path.join(cursors, NAMES[0])
    with open(first, "wb") as f:
        f.write(data)
    for n in NAMES[1:]:
        p = os.path.join(cursors, n)
        if os.path.lexists(p):
            os.remove(p)
        os.symlink(NAMES[0], p)
    with open(os.path.join(dest, "index.theme"), "w") as f:
        f.write("[Icon Theme]\nName=blank\nComment=transparent cursor\n")
    print("blank cursor theme written to", dest)


if __name__ == "__main__":
    main()
