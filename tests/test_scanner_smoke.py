"""Drive the scanner loop with fake camera frames (no window) to check the auto-trigger."""
import os, sys
import cv2, numpy as np
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scanner
from test_synthetic import fake_photo, get_img

scanner.SESSION_PATH = os.path.join(os.path.dirname(__file__), "smoke_session.json")
scanner.SETTINGS_PATH = os.path.join(os.path.dirname(__file__), "smoke_settings.json")
scanner.EXPORT_DIR = os.path.join(os.path.dirname(__file__), "smoke_exports")
for p in (scanner.SESSION_PATH, scanner.SETTINGS_PATH):
    if os.path.exists(p): os.remove(p)

class FakeCap:
    def __init__(self, frames): self.frames, self.i = frames, 0
    def read(self):
        if self.i >= len(self.frames): return False, None
        self.i += 1; return True, self.frames[self.i - 1].copy()
    def release(self): pass
    def isOpened(self): return True

scanner.Scanner.open_camera = lambda self, i: setattr(self, "cam_index", i)
scanner.os.startfile = lambda p: None
s = scanner.Scanner(type("A", (), {"camera": None})())
rng = np.random.default_rng(3)
names = ["Lightning Bolt", "Counterspell", "Llanowar Elves"]
frames, bg = [], None
for n in names:
    card = s.db.by_id([r[0] for r in s.db.db.execute("select id from cards where name=? and lang='en' and image_normal is not null order by released_at desc limit 1", (n,))][0])
    bg, photo = fake_photo(get_img(card), rng)
    frames += [bg] * 15 + [photo] * 40      # empty desk, then card sits still
frames += [bg] * 10
s.cap = FakeCap(frames)
s.zone = [380, 100, 900, 620]
shown = []
scanner.cv2.imshow = lambda *a: shown.append(1)
scanner.cv2.namedWindow = scanner.cv2.setMouseCallback = lambda *a, **k: None
scanner.cv2.getWindowProperty = lambda *a: 1
scanner.cv2.destroyAllWindows = lambda: None
import time
def slow_key(ms):
    time.sleep(0.03)  # let the recogniser thread keep up, like a real 30 fps camera
    if s.cap.i >= len(s.cap.frames):
        while s.busy or not s.results.empty():
            time.sleep(0.05)
            while not s.results.empty(): s.handle_result(s.results.get())
        return ord("q")
    return -1
scanner.cv2.waitKeyEx = slow_key
s.last_frame = frames[0]; s.capture_background(frames[0])
s.run()
got = [s.db.by_id(e["id"])["name"] for e in s.entries]
print("scanned:", got, "review pending:", s.review is not None, "frames drawn:", len(shown))
print("review:", s.review and (s.review["name_text"], s.review["candidates"][:3]))
cv2.imwrite(os.path.join(os.path.dirname(__file__), "dbg_review.jpg"), s.review["card"]) if s.review else None
assert got == names, got
s.export()
print("SMOKE TEST PASSED")
