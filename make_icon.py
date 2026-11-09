"""Generates icon.ico (and icon.png preview) for LLM Explain: a highlighted line of text
with a speech bubble containing a question mark."""
import os
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
S = 1024  # draw large, downsample for crisp small sizes


def font(size):
    for name in ("segoeuib.ttf", "arialbd.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default()


def draw():
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))

    # background: rounded square with vertical blue gradient
    grad = Image.new("RGBA", (S, S))
    gd = ImageDraw.Draw(grad)
    top, bot = (52, 120, 230), (22, 66, 160)
    for y in range(S):
        t = y / (S - 1)
        gd.line([(0, y), (S, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bot)) + (255,))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle((40, 40, S - 40, S - 40), radius=210, fill=255)
    img.paste(grad, (0, 0), mask)

    d = ImageDraw.Draw(img)
    # "text lines", the middle one highlighted in yellow
    line_col = (255, 255, 255, 150)
    d.rounded_rectangle((150, 640, 620, 690), radius=25, fill=line_col)
    d.rounded_rectangle((130, 735, 700, 835), radius=22, fill=(255, 214, 10, 255))  # highlighter
    d.rounded_rectangle((170, 772, 660, 798), radius=13, fill=(30, 60, 140, 255))   # text under highlight
    d.rounded_rectangle((150, 880, 520, 930), radius=25, fill=line_col)

    # speech bubble with "?"
    bx0, by0, bx1, by1 = 470, 120, 900, 520
    d.ellipse((bx0, by0, bx1, by1), fill=(255, 255, 255, 255))
    d.polygon([(560, 470), (500, 610), (660, 500)], fill=(255, 255, 255, 255))
    d.text(((bx0 + bx1) // 2, (by0 + by1) // 2 + 10), "?", font=font(330),
           fill=(26, 90, 200, 255), anchor="mm")
    return img


if __name__ == "__main__":
    big = draw()
    big.resize((256, 256), Image.LANCZOS).save(os.path.join(HERE, "icon.png"))
    big.save(os.path.join(HERE, "icon.ico"),
             sizes=[(16, 16), (20, 20), (24, 24), (32, 32), (40, 40), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("wrote icon.ico and icon.png")
