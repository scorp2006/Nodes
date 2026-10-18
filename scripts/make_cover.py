"""The blog cover: pixel art, 1000x420 (DEV's cover size) rendered at 2x. Standard library only: every pixel is placed
by hand, including a small pixel font and the four "nodes" mascots.

  python scripts/make_cover.py [out.png]
"""
import struct
import sys
import zlib
from pathlib import Path

W, H, S = 1000, 420, 2  # logical size and scale
BG = (255, 255, 255)
CREAM, INK, DARK = (255, 255, 255), (22, 22, 26), (22, 22, 26)
YEL, CORAL, SKY, RED, MINT, PINK = (255, 200, 61), (255, 107, 107), (77, 171, 247), (240, 62, 62), (81, 207, 102), (255, 168, 196)
ORANGE = (255, 146, 43)
WHITE = (255, 255, 255)

px = bytearray(BG * (W * S * H * S))


def rect(x, y, w, h, c):
    x0, y0, x1, y1 = max(0, int(x * S)), max(0, int(y * S)), min(W * S, int((x + w) * S)), min(H * S, int((y + h) * S))
    row = bytes(c) * (x1 - x0)
    for yy in range(y0, y1):
        i = (yy * W * S + x0) * 3
        px[i:i + len(row)] = row


FONT = {  # 5x7 pixel glyphs
    "A": "01110 10001 10001 11111 10001 10001 10001", "B": "11110 10001 10001 11110 10001 10001 11110",
    "C": "01111 10000 10000 10000 10000 10000 01111", "D": "11110 10001 10001 10001 10001 10001 11110",
    "E": "11111 10000 10000 11110 10000 10000 11111", "F": "11111 10000 10000 11110 10000 10000 10000",
    "G": "01111 10000 10000 10011 10001 10001 01111", "H": "10001 10001 10001 11111 10001 10001 10001",
    "I": "11111 00100 00100 00100 00100 00100 11111", "J": "00111 00010 00010 00010 00010 10010 01100",
    "K": "10001 10010 10100 11000 10100 10010 10001", "L": "10000 10000 10000 10000 10000 10000 11111",
    "M": "10001 11011 10101 10101 10001 10001 10001", "N": "10001 11001 10101 10011 10001 10001 10001",
    "O": "01110 10001 10001 10001 10001 10001 01110", "P": "11110 10001 10001 11110 10000 10000 10000",
    "Q": "01110 10001 10001 10001 10101 10010 01101", "R": "11110 10001 10001 11110 10100 10010 10001",
    "S": "01111 10000 10000 01110 00001 00001 11110", "T": "11111 00100 00100 00100 00100 00100 00100",
    "U": "10001 10001 10001 10001 10001 10001 01110", "V": "10001 10001 10001 10001 10001 01010 00100",
    "W": "10001 10001 10001 10101 10101 10101 01010", "X": "10001 10001 01010 00100 01010 10001 10001",
    "Y": "10001 10001 01010 00100 00100 00100 00100", "Z": "11111 00001 00010 00100 01000 10000 11111",
    "0": "01110 10011 10101 10101 11001 10001 01110", "1": "00100 01100 00100 00100 00100 00100 01110",
    "2": "01110 10001 00001 00110 01000 10000 11111", "6": "01110 10000 10000 11110 10001 10001 01110",
    " ": "00000 00000 00000 00000 00000 00000 00000", "-": "00000 00000 00000 11111 00000 00000 00000",
    "+": "00000 00100 00100 11111 00100 00100 00000", ".": "00000 00000 00000 00000 00000 01100 01100",
    "!": "00100 00100 00100 00100 00100 00000 00100", "&": "01100 10010 10100 01000 10101 10010 01101",
    "/": "00001 00010 00010 00100 01000 01000 10000", "'": "00100 00100 01000 00000 00000 00000 00000", "·": "00000 00000 00000 01100 01100 00000 00000",
    "a": "00000 00000 01110 00001 01111 10001 01111", "c": "00000 00000 01110 10000 10000 10001 01110",
    "d": "00001 00001 01101 10011 10001 10001 01111", "e": "00000 00000 01110 10001 11111 10000 01110",
    "g": "00000 00000 01111 10001 01111 00001 01110", "h": "10000 10000 10110 11001 10001 10001 10001",
    "i": "00100 00000 01100 00100 00100 00100 01110", "l": "01100 00100 00100 00100 00100 00100 01110",
    "n": "00000 00000 10110 11001 10001 10001 10001", "o": "00000 00000 01110 10001 10001 10001 01110",
    "s": "00000 00000 01111 10000 01110 00001 11110", "t": "01000 01000 11100 01000 01000 01001 00110",
}


def text(s, x, y, cell, c):
    for ch in s:
        g = FONT[ch].split()
        for r, line in enumerate(g):
            for k, bit in enumerate(line):
                if bit == "1":
                    rect(x + k * cell, y + r * cell, cell, cell, c)
        x += 6 * cell
    return x


def width(s, cell):
    return len(s) * 6 * cell - cell


SPRITES = {  # the "nodes": k outline, b body, w white, p pink, e eye
    "cat": [".k..........k.", "kbk........kbk", "kbbk......kbbk", "kbbbkkkkkkbbbk", "kbbbbbbbbbbbbk", "kbbbbbbbbbbbbk",
            "kbbebbbbbbebbk", "kbbebbbbbbebbk", "kbpbbkbbkbbpbk", "kbbbbbkkbbbbbk", ".kbbbbbbbbbbk.", "..kkkkkkkkkk.."],
    "bunny": ["..kk......kk..", ".kbpk....kbpk.", ".kbpk....kbpk.", ".kbpk....kbpk.", ".kbbkkkkkkbbk.", "kbbbbbbbbbbbbk",
              "kbbebbbbbbebbk", "kbbebbbbbbebbk", "kbpbbkbbkbbpbk", "kbbbbbkkbbbbbk", ".kbbbbbbbbbbk.", "..kkkkkkkkkk.."],
    "bear": [".kkk......kkk.", "kbpbk....kbpbk", "kbbbkkkkkkbbbk", ".kbbbbbbbbbbk.", "kbbbbbbbbbbbbk", "kbbebbbbbbebbk",
             "kbbebbbbbbebbk", "kbbbbwwwwbbbbk", "kbpbwwkkwwbpbk", "kbbbbwwwwbbbbk", ".kbbbbbbbbbbk.", "..kkkkkkkkkk.."],
    "frog": [".kkk......kkk.", "kwwwk....kwwwk", "kwekbkkkkbkewk", "kbbbbbbbbbbbbk", "kbbbbbbbbbbbbk", "kbpbbbbbbbbpbk",
             "kbbkbbbbbbkbbk", "kbbbkkkkkkbbbk", ".kbbbbbbbbbbk.", "..kkkkkkkkkk.."],
}


def sprite(name, x, y, u, body):
    pal = {"k": INK, "b": body, "w": WHITE, "p": PINK, "e": INK}
    for r, line in enumerate(SPRITES[name]):
        for k, ch in enumerate(line):
            if ch in pal:
                rect(x + k * u, y + r * u, u, u, pal[ch])
    if name != "frog":  # a white glint in each eye
        er = next(i for i, ln in enumerate(SPRITES[name]) if "e" in ln)
        for k, ch in enumerate(SPRITES[name][er]):
            if ch == "e":
                rect(x + k * u + u * .5, y + er * u, u * .5, u * .5, WHITE)


def bubble(s, x, y, c):  # a little pixel speech bubble above a mascot
    w = width(s, 3) + 22
    rect(x, y, w, 33, INK)
    rect(x + 3, y + 3, w - 6, 27, c)
    rect(x + 12, y + 33, 9, 4, INK)
    rect(x + 15, y + 37, 3, 4, INK)
    text(s, x + 11, y + 6, 3, INK)


def stairs(x, y, n, dx, dy, c, size=12):
    for i in range(n):
        rect(x + i * dx, y + i * dy, size, size, c)


# the graph: pixel edges (stairs) between pixel nodes
stairs(30, 380, 12, 14, -14, YEL)
stairs(700, 205, 12, 14, -14, YEL)
stairs(190, 22, 5, 14, 0, CORAL, 10)
for i, c in enumerate([YEL, SKY, YEL, CORAL, YEL]):  # bars, like a bus of messages
    rect(40 + i * 26, 8, 12, 48, c)
for x, y, c in [(856, 46, CORAL), (686, 218, SKY), (950, 120, SKY), (900, 300, YEL),
                (120, 300, SKY)]:
    rect(x - 4, y - 4, 20, 20, INK)
    rect(x, y, 12, 12, c)
for x0 in (880, 900, 920):  # a little pixel tower on the right
    for k in range(5):
        rect(x0, 330 + k * 14, 12, 10, [RED, CORAL, PINK][(x0 // 20 + k) % 3])

# the card
cx, cy, cw, ch = 245, 85, 510, 250
rect(cx + 8, cy + 8, cw, ch, DARK)                     # hard pixel shadow
rect(cx - 5, cy - 5, cw + 10, ch + 10, INK)
rect(cx, cy, cw, ch, CREAM)
parts = ["AI AGENTS", "FALKORDB", "MCP"]               # header, split by red dots
hw = sum(width(t, 3) for t in parts) + 40 * (len(parts) - 1)
x = cx + (cw - hw) / 2
for i, t in enumerate(parts):
    x = text(t, x, cy + 22, 3, INK) - 3
    if i < len(parts) - 1:
        rect(x + 16, cy + 29, 8, 8, RED)
        x += 40
title = "NODES"
tx = cx + (cw - width(title, 15) - 7) / 2
for dx, dy, c in [(6, 6, YEL), (0, 0, DARK)]:          # bold: each glyph drawn twice, half a cell apart; yellow shade behind
    for ox, oy in [(0, 0), (7, 0), (0, 7), (7, 7)]:
        text(title, tx + dx + ox, cy + 60 + dy + oy, 15, c)
pill = "Agents that don't collide"
rect(cx + 14, cy + 194, cw - 28, 42, CORAL)
for ox in (0, 1.5):                                     # bold, like the title
    text(pill, cx + (cw - width(pill, 3)) / 2 + ox, cy + 204, 3, (255, 255, 255))

# the nodes: four mascots on the graph, talking like the gate
sprite("cat", 120, 225, 5, SKY)
bubble("GO!", 118, 178, MINT)
sprite("bunny", 772, 72, 5, PINK)
bubble("WAIT", 770, 26, YEL)
sprite("bear", 46, 104, 5, ORANGE)
bubble("HANDOFF", 34, 60, SKY)
sprite("frog", 800, 252, 5, MINT)
bubble("ON IT", 798, 206, PINK)



def png(path):
    w, h = W * S, H * S
    raw = b"".join(b"\x00" + bytes(px[y * w * 3:(y + 1) * w * 3]) for y in range(h))
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)  # noqa: E731
    Path(path).write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                           + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "assets/nodes-cover.png"
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    png(out)
    print(out)
