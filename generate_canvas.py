import os
from PIL import Image, ImageDraw, ImageFont, ImageFilter
import math

def create_cyberpunk_background():
    # 1. Initialize image in RGBA for transparency/layer operations
    width, height = 1920, 1080
    base_color = (8, 5, 18, 255) # Deep dark violet-blue
    img = Image.new("RGBA", (width, height), base_color)
    draw = ImageDraw.Draw(img)

    # Create a separate layer for glows and overlays
    glow_layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow_layer)

    # 2. Draw vertical gradient for ambient background light
    # We want a subtle fade to a slightly lighter violet in the center-top
    for y in range(height):
        # Interpolation factor from top to bottom
        factor = y / height
        # Blend deep violet/black with dark purple
        # Top: (10, 6, 24) -> Bottom: (4, 2, 10)
        r = int(10 * (1 - factor) + 4 * factor)
        g = int(6 * (1 - factor) + 2 * factor)
        b = int(24 * (1 - factor) + 10 * factor)
        draw.line([(0, y), (width, y)], fill=(r, g, b, 255))

    # 3. Add soft radial glows in the background
    # Large central purple glow
    center_x, center_y = width // 2, height // 2
    for r_outer in range(900, 50, -25):
        # Draw concentric transparent ellipses for smooth radial glow
        alpha = int(4 * (1.0 - (r_outer / 900))**2) # Soft falloff
        glow_draw.ellipse(
            [center_x - r_outer, center_y - r_outer, center_x + r_outer, center_y + r_outer],
            fill=(138, 43, 226, alpha) # BlueViolet
        )

    # 4. Draw retro-futuristic grid (perspective)
    # Grid horizon
    horizon_y = 620
    grid_color = (186, 85, 211) # MediumOrchid
    
    # 4a. Horizontal lines with logarithmic-like perspective spacing
    num_horiz_lines = 24
    for i in range(num_horiz_lines):
        # Calculate y coordinate using power curve to simulate perspective depth
        t = i / (num_horiz_lines - 1)
        y = horizon_y + int((height - horizon_y) * (t ** 2.2))
        
        # Grid lines get brighter/more opaque closer to the viewer (bottom)
        # and fade to 0 at the horizon
        alpha = int(10 + 60 * (t ** 1.5))
        glow_draw.line([(0, y), (width, y)], fill=(grid_color[0], grid_color[1], grid_color[2], alpha), width=1)

    # 4b. Perspective vertical lines converging to the center horizon
    num_vert_lines = 40
    # Center of convergence is at (width/2, horizon_y)
    # We want them to spread out at the bottom
    bottom_spacing = width // 15
    for i in range(-num_vert_lines, num_vert_lines + 1):
        x_bottom = (width // 2) + i * bottom_spacing
        x_top = (width // 2) + i * (bottom_spacing // 6) # Slow converge
        
        # Fade vertical lines out towards the left/right and horizon
        dist_factor = 1.0 - min(1.0, abs(i) / num_vert_lines)
        alpha = int(45 * dist_factor)
        
        # Draw the line starting from horizon to bottom
        glow_draw.line([(x_top, horizon_y), (x_bottom, height)], fill=(grid_color[0], grid_color[1], grid_color[2], alpha), width=1)

    # 5. Composite background with glows
    img = Image.alpha_composite(img, glow_layer)
    draw = ImageDraw.Draw(img)

    # Create a new layer for waveforms and text to enable glowing effects
    wave_layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    wave_draw = ImageDraw.Draw(wave_layer)

    # 6. Draw clean blue/cyan waveforms
    # We'll draw 3 beautiful complex waveforms
    wave_configs = [
        # (amplitude, base_freq, micro_freq, phase, color, glow_color, base_y, line_width)
        (80, 0.004, 0.025, 0.0, (0, 240, 255, 255), (0, 240, 255, 40), 610, 3), # Main Cyan wave
        (50, 0.007, 0.04, 2.5, (0, 140, 255, 220), (0, 140, 255, 30), 610, 2),  # Mid Blue wave
        (35, 0.012, 0.07, 4.8, (120, 220, 255, 180), (120, 220, 255, 20), 610, 1) # High-freq accent wave
    ]

    for amp, b_freq, m_freq, phase, color, glow_col, base_y, w_width in wave_configs:
        points = []
        for x in range(0, width + 5, 2):
            # Smooth envelope that tapers to 0 at edges
            # using a sine window
            env = math.sin(math.pi * x / width) ** 2
            
            # Combine main wave and high-frequency noise
            main_sin = math.sin(b_freq * x + phase)
            micro_sin = math.sin(m_freq * x - phase * 1.5)
            
            y_offset = (main_sin * 0.8 + micro_sin * 0.2) * amp * env
            y = base_y + y_offset
            points.append((x, y))
            
        # Draw glow line (wider, low opacity)
        wave_draw.line(points, fill=glow_col, width=w_width + 6)
        # Draw core line (thinner, bright)
        wave_draw.line(points, fill=color, width=w_width)

    # 7. Draw minimalist text 'YOUTUBE MUSIC LIVE'
    # We'll use Bahnschrift.ttf if available
    try:
        font_path = "C:\\Windows\\Fonts\\bahnschrift.ttf"
        font_large = ImageFont.truetype(font_path, 40)
        font_sub = ImageFont.truetype(font_path, 13)
    except IOError:
        # Fallback to default if not found
        font_large = ImageFont.load_default()
        font_sub = ImageFont.load_default()

    # Spaced text function for modern minimalist style
    def draw_spaced_text_centered(draw_obj, text, center_x, y, font, color, spacing):
        char_widths = []
        for char in text:
            # Measure char width
            bbox = draw_obj.textbbox((0, 0), char, font=font)
            char_widths.append(bbox[2] - bbox[0])
            
        total_width = sum(char_widths) + spacing * (len(text) - 1)
        start_x = center_x - total_width / 2
        
        curr_x = start_x
        for i, char in enumerate(text):
            draw_obj.text((curr_x, y), char, font=font, fill=color)
            curr_x += char_widths[i] + spacing

    # Draw Text
    text_color = (255, 255, 255, 255)
    text_glow_color = (0, 240, 255, 30) # Cyan glow for text
    
    text_y = 380
    # Draw a subtle glow behind the text
    for offset_x in [-2, 0, 2]:
        for offset_y in [-2, 0, 2]:
            if offset_x != 0 or offset_y != 0:
                draw_spaced_text_centered(wave_draw, "YOUTUBE MUSIC LIVE", width // 2 + offset_x, text_y + offset_y, font_large, text_glow_color, 16)
                
    # Draw main sharp text
    draw_spaced_text_centered(wave_draw, "YOUTUBE MUSIC LIVE", width // 2, text_y, font_large, text_color, 16)

    # Draw minimalist tech decorations
    # Draw a thin bracket around the text
    bracket_color = (0, 240, 255, 120)
    # Let's define the box size
    box_w, box_h = 750, 90
    box_x1 = (width - box_w) // 2
    box_y1 = text_y - 25
    box_x2 = box_x1 + box_w
    box_y2 = box_y1 + box_h
    
    # Corner brackets (cyberpunk style L-shapes)
    len_corner = 20
    # Top-Left
    wave_draw.line([(box_x1, box_y1), (box_x1 + len_corner, box_y1)], fill=bracket_color, width=1)
    wave_draw.line([(box_x1, box_y1), (box_x1, box_y1 + len_corner)], fill=bracket_color, width=1)
    # Top-Right
    wave_draw.line([(box_x2, box_y1), (box_x2 - len_corner, box_y1)], fill=bracket_color, width=1)
    wave_draw.line([(box_x2, box_y1), (box_x2, box_y1 + len_corner)], fill=bracket_color, width=1)
    # Bottom-Left
    wave_draw.line([(box_x1, box_y2), (box_x1 + len_corner, box_y2)], fill=bracket_color, width=1)
    wave_draw.line([(box_x1, box_y2), (box_x1, box_y2 - len_corner)], fill=bracket_color, width=1)
    # Bottom-Right
    wave_draw.line([(box_x2, box_y2), (box_x2 - len_corner, box_y2)], fill=bracket_color, width=1)
    wave_draw.line([(box_x2, box_y2), (box_x2, box_y2 - len_corner)], fill=bracket_color, width=1)

    # Draw subtitle below
    sub_y = text_y + 95
    draw_spaced_text_centered(wave_draw, "• 24/7 DOWNTEMPO LO-FI ELECTRONIC BEATS •", width // 2, sub_y, font_sub, (186, 85, 211, 200), 4)

    # Composite waveform and text onto base image
    final_img = Image.alpha_composite(img, wave_layer)
    
    # Convert to RGB to save as JPG or PNG without alpha channel if not needed,
    # but PNG supports RGBA and is standard. Let's convert to RGB to ensure max compatibility
    # and clean presentation.
    rgb_img = final_img.convert("RGB")
    
    output_path = "canvas_static.png"
    rgb_img.save(output_path, "PNG")
    print(f"Image successfully generated and saved to {os.path.abspath(output_path)}")

if __name__ == "__main__":
    create_cyberpunk_background()
