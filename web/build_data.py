"""Export the data the phone app needs from data/cards.db (run after update_db.py)."""
import json, os, sys
import onnxruntime as ort

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from carddb import CardDB, normalise

db = CardDB()
pairs = []
for lookup, names in db.by_lookup.items():
    exact = [n for n in names if normalise(n) == lookup]
    pairs.append([lookup, (exact or names)[0]])
os.makedirs(os.path.join(HERE, "data"), exist_ok=True)
with open(os.path.join(HERE, "data", "names.json"), "w", encoding="utf-8") as f:
    json.dump(pairs, f, separators=(",", ":"), ensure_ascii=False)

sess = ort.InferenceSession(os.path.join(HERE, "models", "en_rec.onnx"))
chars = sess.get_modelmeta().custom_metadata_map["character"].split("\n")
with open(os.path.join(HERE, "models", "en_dict.json"), "w", encoding="utf-8") as f:
    json.dump(chars, f, ensure_ascii=False)
print(f"{len(pairs)} names, {len(chars)} OCR characters")


# ---- picture recognition for the phone (needs the trained model: train_model.py) ----
import shutil
import numpy as np

PHONE_DIMS = 96  # fingerprints compressed 512 -> 96 numbers, stored as int8 (tested: no accuracy loss)
ROOT = os.path.dirname(HERE)
model = os.path.join(ROOT, "data", "vis", "card_embed.onnx")
index = os.path.join(ROOT, "data", "card_embed_index.npz")
if os.path.exists(model) and os.path.exists(index):
    d = np.load(index)
    feats = d["feats"].astype(np.float32)
    _, _, vt = np.linalg.svd(feats[::2], full_matrices=False)
    proj = vt[:PHONE_DIMS].T.astype(np.float32)               # 512 x 96
    comp = feats @ proj
    comp /= np.linalg.norm(comp, axis=1, keepdims=True)
    q = np.clip(np.round(comp * 127), -127, 127).astype(np.int8)
    shutil.copy(model, os.path.join(HERE, "models", "card_embed.onnx"))
    q.tofile(os.path.join(HERE, "data", "art_index.bin"))
    proj.tofile(os.path.join(HERE, "data", "art_proj.bin"))
    names = [str(n) for n in d["names"]]
    uniq = sorted(set(names))
    pos = {n: i for i, n in enumerate(uniq)}
    with open(os.path.join(HERE, "data", "art_meta.json"), "w", encoding="utf-8") as f:
        json.dump(dict(dims=PHONE_DIMS, count=len(names), ids=[str(i) for i in d["ids"]],
                       names=uniq, name_of=[pos[n] for n in names]), f, separators=(",", ":"), ensure_ascii=False)
    print(f"picture index: {len(names):,} artworks x {PHONE_DIMS} dims ({q.nbytes / 1e6:.1f} MB)")
