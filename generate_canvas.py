import os
import math
from PIL import Image, ImageDraw, ImageFont


def create_canvas():
    width, height = 1920, 1080

    # ──────────────────────────────────────────────────────────────────────────
    # LAYOUT ZONES (pixel coords, 1920x1080):
    #   Hero zone:       y=100 to y=620  (vinyl art left + track info right, vertically centered)
    #   Viz zone:        y=650 to y=960  (FFmpeg bars overlay: x=60, y=650, 1800x310px)
    #   Footer zone:     y=965 to y=1080
    # ──────────────────────────────────────────────────────────────────────────

    # ── 1. Background: deep dark gradient ─────────────────────────────────────
    img = Image.new("RGBA", (width, height), (5, 4, 14, 255))
    draw = ImageDraw.Draw(img)
    for y in range(height):
        t = y / height
        r = int(8 * (1 - t) + 3 * t)
        g = int(5 * (1 - t) + 3 * t)
        b = int(22 * (1 - t) + 10 * t)
        draw.line([(0, y), (width, y)], fill=(max(2, r), max(2, g), max(8, b), 255))

    # ── 2. Dual ambient glow: one behind art, one behind info ─────────────────
    glow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)

    # Glow behind vinyl art (left)
    for r_out in range(480, 10, -16):
        alpha = int(8 * (1 - r_out / 480) ** 1.8)
        gd.ellipse([340 - r_out, 360 - r_out, 340 + r_out, 360 + r_out],
                   fill=(70, 30, 160, alpha))

    # Glow behind info zone (right, softer)
    for r_out in range(500, 10, -18):
        alpha = int(4 * (1 - r_out / 500) ** 2)
        gd.ellipse([1200 - r_out, 360 - r_out, 1200 + r_out, 360 + r_out],
                   fill=(50, 20, 120, alpha))

    img = Image.alpha_composite(img, glow)

    # ── 3. Decoration layer ────────────────────────────────────────────────────
    dec = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    dd = ImageDraw.Draw(dec)

    # Top accent lines
    dd.line([(0, 1), (width, 1)], fill=(0, 240, 255, 50), width=1)
    dd.line([(0, 4), (width, 4)], fill=(0, 240, 255, 15), width=1)

    # Zone dividers
    dd.line([(60, 95), (width - 60, 95)], fill=(0, 240, 255, 20), width=1)
    dd.line([(60, 640), (width - 60, 640)], fill=(0, 240, 255, 20), width=1)
    dd.line([(60, 963), (width - 60, 963)], fill=(0, 240, 255, 12), width=1)

    # ── 4. Load fonts ──────────────────────────────────────────────────────────
    font_paths = [
        "assets/bahnschrift.ttf",
        "C:\\Windows\\Fonts\\bahnschrift.ttf",
        "/app/assets/bahnschrift.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]
    font_path = None
    for p in font_paths:
        if os.path.exists(p):
            font_path = p
            break

    try:
        if font_path:
            font_sm = ImageFont.truetype(font_path, 11)
            font_md = ImageFont.truetype(font_path, 13)
            font_lg = ImageFont.truetype(font_path, 16)
        else:
            raise IOError()
    except IOError:
        font_sm = font_md = font_lg = ImageFont.load_default()

    # ── 5. Album art placeholder — vinyl record ───────────────────────────────
    # Centered vertically in hero zone (y=100 to y=620 → center y=360)
    # Art box: 440x440px, centered at x=340, y=360
    ART_SIZE = 440
    cx_art, cy_art = 340, 360
    ART_X1 = cx_art - ART_SIZE // 2
    ART_Y1 = cy_art - ART_SIZE // 2
    ART_X2 = ART_X1 + ART_SIZE
    ART_Y2 = ART_Y1 + ART_SIZE

    # Card background glow
    for pad in range(32, 0, -4):
        al = int(35 * (1 - pad / 32) ** 1.8)
        dd.rectangle([ART_X1 - pad, ART_Y1 - pad, ART_X2 + pad, ART_Y2 + pad],
                     outline=(80, 40, 200, al), width=1)
    dd.rectangle([ART_X1, ART_Y1, ART_X2, ART_Y2], fill=(8, 5, 18, 248))

    # Vinyl disc layers (bright, punchy — they need to read at broadcast res)
    vinyl_layers = [
        (210, (22, 16, 44, 255)),   # outer disc fill
        (207, (55, 35, 100, 255)),  # bright groove edge
        (200, (22, 16, 44, 255)),   # back to dark
        (184, (48, 30, 90, 255)),   # groove ring
        (168, (22, 16, 44, 255)),
        (152, (44, 28, 85, 255)),
        (136, (22, 16, 44, 255)),
        (118, (40, 24, 80, 255)),
        (100, (22, 16, 44, 255)),
        (82,  (38, 22, 76, 255)),
        (64,  (22, 16, 44, 255)),
        (50,  (55, 30, 110, 255)),  # label area (brighter purple-blue)
        (46,  (48, 26, 98, 255)),
        (9,   (5, 4, 12, 255)),     # spindle hole
    ]
    for radius, color in vinyl_layers:
        dd.ellipse(
            [cx_art - radius, cy_art - radius, cx_art + radius, cy_art + radius],
            fill=color,
        )

    # Vivid outer glow on vinyl edge
    for g_r in range(220, 202, -1):
        a_g = int(80 * (1 - (g_r - 202) / 18) ** 1.5)
        dd.ellipse(
            [cx_art - g_r, cy_art - g_r, cx_art + g_r, cy_art + g_r],
            outline=(60, 30, 140, a_g), width=1,
        )

    # Cyan shine highlight (top-left of disc)
    for h_r in range(215, 195, -2):
        a_h = int(40 * (1 - (h_r - 195) / 20) ** 2)
        dd.arc(
            [cx_art - h_r, cy_art - h_r, cx_art + h_r, cy_art + h_r],
            start=210, end=280,
            fill=(0, 200, 255, a_h), width=2,
        )

    # Groove radial texture
    for angle_deg in range(0, 360, 8):
        rad = math.radians(angle_deg)
        for r_seg in range(54, 205, 24):
            r_end = min(r_seg + 17, 204)
            x1 = cx_art + r_seg * math.cos(rad)
            y1 = cy_art + r_seg * math.sin(rad)
            x2 = cx_art + r_end * math.cos(rad)
            y2 = cy_art + r_end * math.sin(rad)
            dd.line([(x1, y1), (x2, y2)], fill=(70, 45, 115, 12), width=1)

    # Corner brackets on art card
    CLEN = 20
    BC = (0, 240, 255, 120)
    for bx, by in [(ART_X1, ART_Y1), (ART_X2, ART_Y1), (ART_X1, ART_Y2), (ART_X2, ART_Y2)]:
        dx = CLEN if bx == ART_X1 else -CLEN
        dy = CLEN if by == ART_Y1 else -CLEN
        dd.line([(bx, by), (bx + dx, by)], fill=BC, width=2)
        dd.line([(bx, by), (bx, by + dy)], fill=BC, width=2)

    # "ALBUM ART" micro-label
    dd.text((cx_art, ART_Y2 - 18), "[ ALBUM ART ]", font=font_sm,
            fill=(0, 240, 255, 38), anchor="mm")

    # ── 6. Track info zone (right panel, full height) ─────────────────────────
    # Info zone: x=600 to x=1860, y=100 to y=620
    INFO_X = 620
    INFO_Y_START = 120

    # Vertical separator line
    dd.line([(INFO_X - 22, 108), (INFO_X - 22, 622)], fill=(0, 240, 255, 16), width=1)

    # "NOW PLAYING" label
    dd.text((INFO_X, INFO_Y_START + 20), "NOW PLAYING", font=font_md,
            fill=(186, 85, 211, 200))
    dd.line([(INFO_X, INFO_Y_START + 43), (INFO_X + 310, INFO_Y_START + 43)],
            fill=(186, 85, 211, 65), width=1)

    # Guide labels for FFmpeg drawtext
    # TRACK label y=230 → FFmpeg title at y=244, fontsize=40
    # ARTIST label y=308 → FFmpeg artist at y=320, fontsize=26
    dd.text((INFO_X, 230), "TRACK", font=font_sm, fill=(0, 240, 255, 40))
    dd.text((INFO_X, 308), "ARTIST", font=font_sm, fill=(0, 240, 255, 30))

    # ── Big empty space for FFmpeg title (y≈244, size 40) ──
    # ── Big empty space for FFmpeg artist (y≈320, size 26) ──

    # Divider before broadcast block
    dd.line([(INFO_X, 410), (width - 90, 410)], fill=(0, 240, 255, 14), width=1)

    # Broadcast info block
    dd.text((INFO_X, 422), "BROADCAST INFO", font=font_sm, fill=(186, 85, 211, 90))
    bcast_lines = [
        "ENCODER :  H.264 (libx264)  /  AAC @ 192 KBPS",
        "FORMAT  :  RTMP  →  YouTube Live Ingest",
        "SCHEDULE:  24 / 7  CONTINUOUS BROADCAST",
    ]
    for i, line in enumerate(bcast_lines):
        dd.text((INFO_X, 444 + i * 22), line, font=font_sm, fill=(255, 255, 255, 70))

    # ── 7. Visualizer zone frame ───────────────────────────────────────────────
    VIZ_X1, VIZ_Y1 = 60, 650
    VIZ_X2, VIZ_Y2 = width - 60, 958

    # Dark fill
    dd.rectangle([VIZ_X1, VIZ_Y1, VIZ_X2, VIZ_Y2], fill=(0, 0, 0, 75),
                 outline=(0, 240, 255, 25), width=1)

    # Scanline atmosphere
    for sy in range(VIZ_Y1 + 2, VIZ_Y2, 5):
        dd.line([(VIZ_X1 + 1, sy), (VIZ_X2 - 1, sy)], fill=(0, 240, 255, 3), width=1)

    # Corner brackets
    VIZ_C = 22
    VBC = (0, 240, 255, 80)
    for vx, vy in [(VIZ_X1, VIZ_Y1), (VIZ_X2, VIZ_Y1), (VIZ_X1, VIZ_Y2), (VIZ_X2, VIZ_Y2)]:
        dx = VIZ_C if vx == VIZ_X1 else -VIZ_C
        dy = VIZ_C if vy == VIZ_Y1 else -VIZ_C
        dd.line([(vx, vy), (vx + dx, vy)], fill=VBC, width=2)
        dd.line([(vx, vy), (vx, vy + dy)], fill=VBC, width=2)

    # Zone label
    dd.text((VIZ_X1 + 8, VIZ_Y1 + 5), "[ LIVE VISUALIZER ]", font=font_sm,
            fill=(0, 240, 255, 60))

    # ── 8. Footer ──────────────────────────────────────────────────────────────
    footer_items = [
        (60,          "AUDIO: AAC 192KBPS"),
        (340,         "VIDEO: H.264 1080P"),
        (620,         "SCHEDULE: 24/7 CONTINUOUS"),
        (width - 310, "STREAM: LIVE  ●"),
    ]
    for fx, ft in footer_items:
        dd.text((fx, 986), ft, font=font_sm, fill=(255, 255, 255, 48))

    # ── 9. Composite and save ──────────────────────────────────────────────────
    final = Image.alpha_composite(img, dec)
    rgb = final.convert("RGB")

    out_path = "canvas_static.png"
    if os.path.exists("assets"):
        out_path = os.path.join("assets", "canvas_static.png")

    rgb.save(out_path, "PNG")
    print(f"Canvas saved: {os.path.abspath(out_path)}")
    print(f"  Vinyl art placeholder: cx={cx_art}, cy={cy_art}  ({ART_SIZE}x{ART_SIZE}px)")
    print(f"  Track info (FFmpeg):   x=620, y=244 (title, size 40) / y=320 (artist, size 26)")
    print(f"  Viz overlay zone:      x=60,  y=650  (1800x308px)")


if __name__ == "__main__":
    create_canvas()
