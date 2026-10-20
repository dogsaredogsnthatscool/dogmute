#!/usr/bin/env python3
"""Draws dogmute/static/icon.png: a Win95 window on teal,
the dog inside, with a muted speaker."""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent.parent / "dogmute" / "static" / "icon.png"
EMOJI = "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf"
BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def emoji(ch, size):
    f = ImageFont.truetype(EMOJI, 109)               # the colour font only comes in this size
    im = Image.new("RGBA", (136, 128), (0, 0, 0, 0))
    ImageDraw.Draw(im).text((0, 0), ch, font=f, embedded_color=True)
    return im.crop(im.getbbox()).resize((size, size), Image.LANCZOS)


im = Image.new("RGBA", (256, 256), (0, 128, 128, 255))
d = ImageDraw.Draw(im)
d.rectangle((20, 26, 238, 234), fill=(0, 0, 0))                     # shadow
d.rectangle((16, 22, 234, 230), fill=(192, 192, 192))              # the window
d.line((16, 22, 234, 22), fill=(255, 255, 255), width=2)
d.line((16, 22, 16, 230), fill=(255, 255, 255), width=2)
for x in range(22, 229):                                          # navy → blue title bar
    t = (x - 22) / 206
    d.line((x, 28, x, 58), fill=(int(0 + 14 * t), int(16 + 107 * t), int(107 + 107 * t)))
d.text((32, 31), "DogMute", font=ImageFont.truetype(BOLD, 21), fill=(255, 255, 255))
d.rectangle((24, 64, 226, 222), fill=(0, 0, 0))
im.alpha_composite(emoji("🐶", 118), (46, 78))
im.alpha_composite(emoji("🔇", 70), (146, 142))
im.save(OUT)
print("wrote", OUT)
