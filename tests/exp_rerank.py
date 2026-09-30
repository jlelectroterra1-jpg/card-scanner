"""Experiment: MobileNet shortlist + close image comparison re-ranking."""
import os, sys, time, pickle
import cv2, numpy as np
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT); sys.path.insert(0, HERE)
from build_visual_index import artwork_list, IMG_DIR
import visual

CACHE = os.path.join(HERE, "exp_cache.pkl")
N_INDEX, N_Q, K = 5000, 150, int(os.environ.get('K', 30))
if not os.path.exists(CACHE):
    from carddb import CardDB
    from recognizer import Recognizer
    from test_synthetic import get_img
    from test_visual import desk, far_shot
    rng = np.random.default_rng(1)
    arts = [a for a in artwork_list() if os.path.exists(os.path.join(IMG_DIR, a["id"] + ".jpg"))]
    arts = [arts[i] for i in rng.choice(len(arts), N_INDEX, replace=False)]
    db = CardDB(); qrng = np.random.default_rng(9)
    qidx = qrng.choice(N_INDEX, N_Q, replace=False); queries = []
    for i in qidx:
        c = db.by_id(arts[i]["id"])
        shot, (x0, y0, x1, y1), empty = far_shot(get_img(c), desk(qrng), qrng)
        st = Recognizer.find_card(shot[y0:y1, x0:x1], empty[y0:y1, x0:x1])
        queries.append(st if st is not None else cv2.resize(shot[y0:y1, x0:x1], (630, 880)))
    imgs = [cv2.imread(os.path.join(IMG_DIR, a["id"] + ".jpg")) for a in arts]
    I = visual.fingerprints(imgs); Q = visual.fingerprints(queries)
    pickle.dump(dict(arts=arts, qidx=qidx, queries=queries, I=I, Q=Q), open(CACHE, "wb"))
d = pickle.load(open(CACHE, "rb"))
arts, qidx, queries, I, Q = d["arts"], d["qidx"], d["queries"], d["I"], d["Q"]
names = np.array([a["name"] for a in arts])
imgs = {}
def ref(i):
    if i not in imgs: imgs[i] = cv2.imread(os.path.join(IMG_DIR, arts[i]["id"] + ".jpg"))
    return imgs[i]

sims = Q @ I.T
short = np.argsort(-sims, 1)[:, :K]
print("mobilenet top1", sum(names[short[k, 0]] == names[qidx[k]] for k in range(N_Q)),
      " recall@5", sum(names[qidx[k]] in names[short[k, :5]] for k in range(N_Q)),
      f" recall@{K}", sum(names[qidx[k]] in names[short[k]] for k in range(N_Q)))

W, H = 73, 102
clahe = cv2.createCLAHE(2.0, (4, 4))
def gray(img):
    g = cv2.cvtColor(cv2.resize(img, (W, H), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY)
    g = clahe.apply(g).astype(np.float32)
    return (g - g.mean()) / (g.std() + 1e-3)
def lab(img, w=24, h=33):
    x = cv2.cvtColor(cv2.resize(img, (w, h), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2LAB).astype(np.float32)
    x = x.reshape(-1, 3); return ((x - x.mean(0)) / (x.std(0) + 1e-3)).ravel()

def ncc(a, b): return float((a * b).mean())
def robust(a, b): return -float(np.median(np.abs(a - b)))

def ecc_ncc(q, r):
    """Align reference r to query q (affine, ECC) then NCC."""
    warp = np.eye(2, 3, dtype=np.float32)
    try:
        _, warp = cv2.findTransformECC(r, q, warp, cv2.MOTION_AFFINE,
                                       (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, 1e-4), None, 3)
        r2 = cv2.warpAffine(r, warp, (W, H), flags=cv2.INTER_LINEAR + cv2.WARP_INVERSE_MAP, borderMode=cv2.BORDER_REFLECT)
    except cv2.error:
        r2 = r
    return ncc(q, r2)

def evaluate(label, score_fn, combine=None):
    t = time.time(); top1 = 0
    for k in range(N_Q):
        qq = queries[k]
        sc = np.array([score_fn(qq, ref(i)) for i in short[k]])
        if combine: sc = combine(sc, sims[k, short[k]])
        top1 += names[short[k][int(np.argmax(sc))]] == names[qidx[k]]
    print(f"{label:34} top1 {top1}/{N_Q}   {(time.time()-t)/N_Q*1000:.0f} ms/query", flush=True)

gq = {}
def gcache(img):
    key = id(img)
    if key not in gq: gq[key] = gray(img)
    return gq[key]
lq = {}
def lcache(img):
    key = id(img)
    if key not in lq: lq[key] = lab(img)
    return lq[key]


def labsig(img, w, h):
    x = cv2.cvtColor(cv2.resize(img, (w, h), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2LAB).astype(np.float32).reshape(-1, 3)
    return (x - x.mean(0)) / (x.std(0) + 1e-3)
cache = {}
def sig(img, w, h):
    key = (id(img), w, h)
    if key not in cache: cache[key] = labsig(img, w, h)
    return cache[key]
def trimmed(q, r, keep=0.75):
    d = np.abs(q - r).sum(1)
    k = int(len(d) * keep)
    return -float(np.partition(d, k)[:k].mean())

for w, h in ((16, 22), (24, 33), (32, 44)):
    evaluate(f"lab L1 {w}x{h}", lambda q, r, w=w, h=h: -float(np.abs(sig(q, w, h) - sig(r, w, h)).mean()))
for keep in (0.6, 0.75, 0.9):
    evaluate(f"lab trimmed {keep} 24x33", lambda q, r, keep=keep: trimmed(sig(q, 24, 33), sig(r, 24, 33), keep))
evaluate("lab trimmed .75 + mobilenet", lambda q, r: trimmed(sig(q, 24, 33), sig(r, 24, 33), 0.75), lambda a, b: a + 3 * b)
evaluate("lab trimmed .75 + 6*mobilenet", lambda q, r: trimmed(sig(q, 24, 33), sig(r, 24, 33), 0.75), lambda a, b: a + 6 * b)

if os.environ.get("PCA"):
    I0, Q0 = I.copy(), Q.copy()
    for dims in (256, 512, 768):
        mean = I0.mean(0); _, _, vt = np.linalg.svd(I0 - mean, full_matrices=False); P = vt[:dims].T
        Ip = (I0 - mean) @ P; Ip /= np.linalg.norm(Ip, axis=1, keepdims=True)
        Qp = (Q0 - mean) @ P; Qp /= np.linalg.norm(Qp, axis=1, keepdims=True)
        sims = Qp @ Ip.T; short = np.argsort(-sims, 1)[:, :K]
        print(f"PCA {dims}: recall@{K}", sum(names[qidx[k]] in names[short[k]] for k in range(N_Q)), end="  ")
        evaluate(f"  +lab trimmed + mobilenet", lambda q, r: trimmed(sig(q, 24, 33), sig(r, 24, 33), 0.75), lambda a, b: a + 3 * b)
    for dims in (512,):
        # no mean-centering: keeps raw cosine geometry
        _, _, vt = np.linalg.svd(I0, full_matrices=False); P = vt[:dims].T
        Ip = I0 @ P; Ip /= np.linalg.norm(Ip, axis=1, keepdims=True); Qp = Q0 @ P; Qp /= np.linalg.norm(Qp, axis=1, keepdims=True)
        sims = Qp @ Ip.T; short = np.argsort(-sims, 1)[:, :K]
        print(f"uncentred {dims}: recall@{K}", sum(names[qidx[k]] in names[short[k]] for k in range(N_Q)), end="  ")
        evaluate(f"  +lab trimmed + mobilenet", lambda q, r: trimmed(sig(q, 24, 33), sig(r, 24, 33), 0.75), lambda a, b: a + 3 * b)
