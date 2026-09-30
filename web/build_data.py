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
