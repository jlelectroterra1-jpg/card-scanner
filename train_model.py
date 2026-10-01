"""Train a card-recognition model that works on small, blurry webcam views of cards.

    python train_model.py              (needs PyTorch; uses the NVIDIA GPU if there is one)

Every training example is a freshly generated "webcam shot" of one of the ~49k card
artworks in data/img: shrunk to 100-230 px tall, tilted, blurred, glare, sleeve edges,
a finger, colour casts, JPEG, and an imperfect crop. A MobileNetV3 learns to give
every shot of the same artwork the same fingerprint (CosFace loss, one class per
artwork). The result is exported to data/vis/card_embed.onnx plus a fingerprint of
every clean card image in data/card_embed_index.npz, which visual.py uses.
"""
import math
import os
import sys
import time

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
IMG_DIR = os.path.join(HERE, "data", "img")
OUT_MODEL = os.path.join(HERE, "data", "vis", "card_embed.onnx")
OUT_INDEX = os.path.join(HERE, "data", "card_embed_index.npz")
CKPT = os.path.join(HERE, "data", "vis", "card_embed.pt")

IN_W, IN_H = 128, 176   # model input (card shape)
EMB = 512
MEAN = np.array([0.485, 0.456, 0.406], np.float32)
STD = np.array([0.229, 0.224, 0.225], np.float32)


# ---------------------------------------------------------------- input prep (shared with visual.py)

def to_input(card_bgr):
    """Straightened card image -> model input (3, IN_H, IN_W) float32."""
    rgb = cv2.cvtColor(cv2.resize(card_bgr, (IN_W, IN_H), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2RGB)
    return (((rgb.astype(np.float32) / 255.0) - MEAN) / STD).transpose(2, 0, 1)


# ---------------------------------------------------------------- fake webcam shots

def _background(rng, w, h, others):
    kind = rng.random()
    if kind < 0.4 and others is not None:  # other cards / busy playmat art
        bg = cv2.resize(others[rng.integers(len(others))], (w, h))
        return cv2.GaussianBlur(bg, (0, 0), rng.uniform(0, 3))
    if kind < 0.7:  # smooth colourful playmat
        small = rng.integers(0, 255, (rng.integers(2, 6), rng.integers(2, 6), 3), dtype=np.uint8)
        return cv2.resize(small, (w, h), interpolation=cv2.INTER_CUBIC)
    base = rng.integers(0, 255, 3)
    return np.clip(base + rng.normal(0, 12, (h, w, 3)), 0, 255).astype(np.uint8)


def webcam_shot(card, rng, others=None):
    """card: clean BGR card image. Returns a degraded, re-cropped version, IN_W x IN_H."""
    ch = rng.uniform(95, 240)                      # card height in camera pixels
    cw = ch * 63 / 88
    pad = int(ch * 0.35)
    W, H = int(cw + 2 * pad), int(ch + 2 * pad)
    canvas = _background(rng, W, H, others)

    # Sleeve: a dark/coloured border around the card, sometimes glossy.
    src = card
    if rng.random() < 0.5:
        b = int(card.shape[0] * rng.uniform(0.01, 0.04))
        colour = [int(c) for c in (rng.integers(0, 60, 3) if rng.random() < 0.7 else rng.integers(0, 255, 3))]
        src = cv2.copyMakeBorder(card, b, b, b, b, cv2.BORDER_CONSTANT, value=colour)

    # Place it with perspective + rotation.
    cx, cy = W / 2 + rng.uniform(-0.05, 0.05) * cw, H / 2 + rng.uniform(-0.05, 0.05) * ch
    ang = math.radians(rng.uniform(-12, 12))
    pts = np.array([[-cw / 2, -ch / 2], [cw / 2, -ch / 2], [cw / 2, ch / 2], [-cw / 2, ch / 2]])
    R = np.array([[math.cos(ang), -math.sin(ang)], [math.sin(ang), math.cos(ang)]])
    dst = (pts @ R.T + [cx, cy] + rng.normal(0, ch * 0.02, (4, 2))).astype(np.float32)
    sh, sw = src.shape[:2]
    M = cv2.getPerspectiveTransform(np.float32([[0, 0], [sw, 0], [sw, sh], [0, sh]]), dst)
    warped = cv2.warpPerspective(src, M, (W, H), flags=cv2.INTER_AREA)
    mask = cv2.warpPerspective(np.full((sh, sw), 255, np.uint8), M, (W, H))
    img = np.where(mask[..., None] > 0, warped, canvas).astype(np.float32)

    # Lighting, colour cast, saturation, gamma.
    img = img * rng.uniform(0.55, 1.35) + rng.uniform(-35, 35)
    img *= rng.uniform(0.8, 1.2, 3)
    if rng.random() < 0.5:
        g = img.mean(2, keepdims=True)
        img = g + (img - g) * rng.uniform(0.5, 1.3)
    if rng.random() < 0.5:  # uneven light across the card
        yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
        img *= 1 + rng.uniform(-0.25, 0.25) * ((xx / W - 0.5) * rng.uniform(-1, 1) + (yy / H - 0.5) * rng.uniform(-1, 1))[..., None]
    if rng.random() < 0.5:  # glare spot
        yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
        gx, gy = cx + rng.uniform(-0.4, 0.4) * cw, cy + rng.uniform(-0.4, 0.4) * ch
        sx, sy = cw * rng.uniform(0.1, 0.5), ch * rng.uniform(0.05, 0.3)
        img += rng.uniform(80, 220) * np.exp(-(((xx - gx) / sx) ** 2 + ((yy - gy) / sy) ** 2))[..., None]
    img = np.clip(img, 0, 255).astype(np.uint8)
    if rng.random() < 0.3:  # a finger over an edge
        fx = int(cx + rng.choice([-1, 1]) * cw * rng.uniform(0.3, 0.55))
        fy = int(cy + rng.uniform(-0.5, 0.5) * ch)
        skin = [int(v) for v in (rng.integers(60, 140), rng.integers(100, 170), rng.integers(150, 230))]
        cv2.ellipse(img, (fx, fy), (int(cw * rng.uniform(0.1, 0.2)), int(ch * rng.uniform(0.12, 0.25))),
                    rng.uniform(0, 180), 0, 360, skin, -1)

    # Camera: blur (focus / motion), noise, JPEG.
    if rng.random() < 0.3:
        k = int(rng.integers(3, 8))
        kern = np.zeros((k, k), np.float32)
        kern[k // 2] = 1
        kern = cv2.warpAffine(kern, cv2.getRotationMatrix2D((k / 2, k / 2), rng.uniform(0, 180), 1), (k, k))
        img = cv2.filter2D(img, -1, kern / max(kern.sum(), 1e-3))
    img = cv2.GaussianBlur(img, (0, 0), rng.uniform(0.3, 1.6))
    img = np.clip(img + rng.normal(0, rng.uniform(1, 7), img.shape), 0, 255).astype(np.uint8)
    img = cv2.imdecode(cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, int(rng.integers(35, 92))])[1], 1)

    # The scanner's crop is never perfect: jitter the corners we "found".
    found = dst + rng.normal(0, ch * 0.025, (4, 2)).astype(np.float32)
    if rng.random() < 0.15:  # sometimes it grabbed some table too
        found += (found - found.mean(0)) * rng.uniform(0.03, 0.12)
    out = cv2.warpPerspective(img, cv2.getPerspectiveTransform(found.astype(np.float32),
                              np.float32([[0, 0], [IN_W, 0], [IN_W, IN_H], [0, IN_H]])), (IN_W, IN_H),
                              flags=cv2.INTER_AREA)
    return out


# ---------------------------------------------------------------- training

# Torch is only needed for training, so the scanner can import webcam_shot/to_input without it.
try:
    import torch
    import torch.nn as nn
    import torch.nn.functional as F
    import torchvision
    from torch.utils.data import Dataset
except ImportError:  # pragma: no cover
    torch = None
    Dataset = object


def training_ids():
    from build_visual_index import artwork_list
    arts = [a for a in artwork_list() if os.path.exists(os.path.join(IMG_DIR, a["id"] + ".jpg"))]
    return [a["id"] for a in arts], [a["name"] for a in arts]


class Shots(Dataset):
    """One freshly generated webcam shot of artwork i per item."""
    def __init__(self, ids):
        self.ids = ids

    def __len__(self):
        return len(self.ids)

    def __getitem__(self, i):
        rng = np.random.default_rng()
        card = cv2.imread(os.path.join(IMG_DIR, self.ids[i] + ".jpg"))
        if card is None:
            card = np.zeros((204, 146, 3), np.uint8)
        others = None
        if rng.random() < 0.4:
            other = cv2.imread(os.path.join(IMG_DIR, self.ids[int(rng.integers(len(self.ids)))] + ".jpg"))
            others = [other] if other is not None else None
        return torch.from_numpy(to_input(webcam_shot(card, rng, others))), i


if torch is not None:
    class Net(nn.Module):
        def __init__(self, pretrained=True):
            super().__init__()
            m = torchvision.models.mobilenet_v3_large(weights="IMAGENET1K_V2" if pretrained else None)
            self.features = m.features
            self.head = nn.Sequential(nn.Linear(960, EMB), nn.BatchNorm1d(EMB))

        def forward(self, x):
            x = self.features(x).mean((2, 3))
            return F.normalize(self.head(x), dim=1)

    class CosFace(nn.Module):
        def __init__(self, n, s=30.0, m=0.25):
            super().__init__()
            self.W = nn.Parameter(torch.randn(n, EMB) * 0.01)
            self.s, self.m = s, m

        def forward(self, emb, y):
            logits = emb @ F.normalize(self.W, dim=1).t()
            logits = logits - self.m * F.one_hot(y, logits.shape[1])
            return F.cross_entropy(self.s * logits, y)


def main():
    from torch.utils.data import DataLoader

    dev = "cuda" if torch.cuda.is_available() else "cpu"
    print("Training on", torch.cuda.get_device_name(0) if dev == "cuda" else "CPU (slow!)", flush=True)
    ids, names = training_ids()
    n_cls = len(ids)
    print(f"{n_cls:,} artworks", flush=True)

    net, loss_fn = Net().to(dev), CosFace(n_cls).to(dev)
    epochs = int(os.environ.get("EPOCHS", 30))
    opt = torch.optim.AdamW([{"params": net.parameters(), "lr": 1e-3}, {"params": loss_fn.parameters(), "lr": 1e-2}],
                            weight_decay=1e-4)
    torch.backends.cudnn.benchmark = False  # its trial runs grab extra GPU memory
    loader = DataLoader(Shots(ids), batch_size=int(os.environ.get("BATCH", 128)), shuffle=True, num_workers=int(os.environ.get("WORKERS", 12)),
                        persistent_workers=True, pin_memory=True, drop_last=True)
    steps = epochs * len(loader)
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=[1e-3, 1e-2], total_steps=steps, pct_start=0.1)
    # Half-precision gives NaNs on GTX 16xx cards, so train in full precision.
    amp = dev == "cuda" and os.environ.get("AMP") == "1"
    scaler = torch.amp.GradScaler(enabled=amp)
    start = 0
    if os.path.exists(CKPT) and "--fresh" not in sys.argv:
        ck = torch.load(CKPT, map_location=dev)
        if ck.get("n_cls") == n_cls and ck.get("epochs") == epochs:
            net.load_state_dict(ck["net"]); loss_fn.load_state_dict(ck["loss"])
            opt.load_state_dict(ck["opt"]); sched.load_state_dict(ck["sched"]); start = ck["epoch"] + 1
            print(f"Resuming from epoch {start}", flush=True)
    t0 = time.time()
    for ep in range(start, epochs):
        net.train()
        tot, n, tb = 0.0, 0, time.time()
        for b, (x, y) in enumerate(loader):
            x, y = x.to(dev, non_blocking=True), y.to(dev, non_blocking=True)
            with torch.autocast(dev, enabled=amp):
                emb = net(x)
            loss = loss_fn(emb.float(), y)
            opt.zero_grad(set_to_none=True)
            scaler.scale(loss).backward()
            scaler.step(opt)
            scaler.update()
            sched.step()
            tot += loss.item() * len(y); n += len(y)
            if b % 100 == 99:
                rate = n / (time.time() - tb)
                mem = torch.cuda.max_memory_reserved() / 2**30 if dev == "cuda" else 0
                print(f"  epoch {ep + 1} step {b + 1}/{len(loader)}  loss {tot / n:.3f}  {rate:.0f} shots/s  gpu mem {mem:.1f} GB", flush=True)
        el = time.time() - t0
        print(f"epoch {ep + 1}/{epochs}  loss {tot / n:.3f}  ({el / 60:.0f} min, ~{el / (ep + 1 - start) * (epochs - ep - 1) / 60:.0f} min left)", flush=True)
        torch.save(dict(net=net.state_dict(), loss=loss_fn.state_dict(), opt=opt.state_dict(),
                        sched=sched.state_dict(), epoch=ep, n_cls=n_cls, epochs=epochs), CKPT)

    export(net, ids, names, dev)


def export(net, ids, names, dev):
    import torch
    net.eval()
    os.makedirs(os.path.dirname(OUT_MODEL), exist_ok=True)
    torch.onnx.export(net.cpu(), torch.zeros(1, 3, IN_H, IN_W), OUT_MODEL, input_names=["image"],
                      output_names=["embedding"], dynamic_axes={"image": {0: "n"}, "embedding": {0: "n"}},
                      opset_version=17, dynamo=False)
    net.to(dev)
    print("Fingerprinting every clean card image...")
    feats = []
    with torch.no_grad():
        for i in range(0, len(ids), 256):
            batch = []
            for cid in ids[i:i + 256]:
                img = cv2.imread(os.path.join(IMG_DIR, cid + ".jpg"))
                batch.append(to_input(img if img is not None else np.zeros((204, 146, 3), np.uint8)))
            feats.append(net(torch.from_numpy(np.stack(batch)).to(dev)).float().cpu().numpy())
    np.savez_compressed(OUT_INDEX, feats=np.vstack(feats).astype(np.float16), ids=np.array(ids), names=np.array(names))
    print(f"Saved {OUT_MODEL} and {OUT_INDEX}")


if __name__ == "__main__":
    if "--export-only" in sys.argv:
        ck = torch.load(CKPT, map_location="cpu")
        net = Net(pretrained=False)
        net.load_state_dict(ck["net"])
        ids, names = training_ids()
        export(net.to("cuda" if torch.cuda.is_available() else "cpu"), ids, names,
               "cuda" if torch.cuda.is_available() else "cpu")
    else:
        main()
