import pyray as pr
from PIL import Image, ImageSequence
import tempfile
import os

class AnimatedGIF:
    def __init__(self, filename, width, height, frame_delay=100):
        self.frame_delay = frame_delay / 1000.0 
        self.last_update = pr.get_time()
        self.current_frame = 0
        self.frames = []
        
        try:
            pil_image = Image.open(filename)
            for frame in ImageSequence.Iterator(pil_image):
                frame_rgba = frame.convert("RGBA")
                
                with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tmp:
                    frame_rgba.save(tmp.name, format='PNG')
                    tmp_name = tmp.name
                
                img = pr.load_image(tmp_name)
                
                # FIX 1: Use Nearest-Neighbor scaling instead of Bilinear so pixel art stays sharp
                pr.image_resize_nn(img, int(width), int(height)) 
                
                tex = pr.load_texture_from_image(img)
                
                # FIX 2: Apply Point Filtering to the GPU texture to prevent edge blurring
                pr.set_texture_filter(tex, pr.TEXTURE_FILTER_POINT)
                
                self.frames.append(tex)
                
                pr.unload_image(img)
                os.remove(tmp_name)
                
        except Exception as e:
            print(f"Error loading GIF '{filename}': {e}")
            img = pr.gen_image_color(int(width), int(height), (0, 0, 0, 0))
            tex = pr.load_texture_from_image(img)
            self.frames.append(tex)
            pr.unload_image(img)

    def update(self):
        if not self.frames or len(self.frames) <= 1:
            return
            
        now = pr.get_time()
        if now - self.last_update > self.frame_delay:
            self.current_frame = (self.current_frame + 1) % len(self.frames)
            self.last_update = now

    def draw(self, position):
        if self.frames:
            pr.draw_texture(self.frames[self.current_frame], int(position[0]), int(position[1]), (255, 255, 255, 255))