"""Reference turnaround calibration: maps each drawn view to world coordinates.

World: meters, Z up, the character faces -Y. Each view is treated as an
orthographic camera looking along `d`, with image-right = `R` and image-up = +Z.
"""
import math
import os

import numpy as np
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
REF = os.path.join(os.path.dirname(HERE), "reference", "turnaround.png")
HEIGHT = 1.76          # floor to top of bun, meters

# x ranges of the five figures on the sheet and the camera azimuth of each view.
# azimuth follows build_character.render_views: camera at (sin a, -cos a) * dist
FIGURES = [
    ("front", (137, 402), 0.0),
    ("threequarter", (448, 698), -32.0),
    ("side", (740, 928), -90.0),
    ("back", (999, 1249), 180.0),
    ("back_threequarter", (1307, 1559), -142.0),
]


def _segments(row):
    cols = np.where(row)[0]
    if len(cols) == 0:
        return []
    segs, s, p = [], cols[0], cols[0]
    for c in cols[1:]:
        if c - p > 1:
            segs.append((s, p))
            s = c
        p = c
    segs.append((s, p))
    return segs


class View:
    def __init__(self, name, rgba, az_deg, mirror=False):
        self.name = name
        if mirror:
            rgba = rgba[:, ::-1].copy()
        self.rgba = rgba
        self.mask = rgba[:, :, 3] > 127
        self.set_azimuth(az_deg)
        rows = np.where(self.mask.any(1))[0]
        self.top, self.floor = rows.min(), rows.max() + 1
        self.s = HEIGHT / (self.floor - self.top)
        # horizontal origin: midway between the two legs at knee height
        r = int(round(self.floor - 0.5 / self.s))
        segs = _segments(self.mask[r])
        big = [sg for sg in segs if sg[1] - sg[0] > 10]
        if len(big) >= 2:
            self.u0 = (sum((a + b) / 2 for a, b in big[:2])) / 2
        else:
            a, b = big[0]
            self.u0 = (a + b) / 2
        self.h, self.w = self.mask.shape

    def set_azimuth(self, az_deg):
        self.az = az_deg
        a = math.radians(az_deg)
        cam = np.array([math.sin(a), -math.cos(a), 0.0])
        self.d = -cam                              # camera looks toward the subject
        self.R = np.cross(self.d, [0, 0, 1.0])     # image right
        self.R /= np.linalg.norm(self.R)

    def project(self, P):
        """P: (N,3) world points -> (N,2) pixel coords (u right, v down)."""
        P = np.asarray(P, dtype=float)
        u = self.u0 + (P @ self.R) / self.s
        v = self.floor - P[:, 2] / self.s
        return np.stack([u, v], 1)

    def depth(self, P):
        return np.asarray(P, dtype=float) @ self.d

    def unproject_dir(self):
        return self.d


CALIB = os.path.join(os.path.dirname(HERE), "reference", "calibration.json")


def load_views(include_mirror=True, calibrated=True):
    img = np.array(Image.open(REF).convert("RGBA"))
    views = {}
    for name, (x0, x1), az in FIGURES:
        crop = img[:, x0 - 20:x1 + 21]
        views[name] = View(name, crop, az)
    if include_mirror:
        # the character is (nearly) symmetric: the right profile mirrored is the left profile
        x0, x1 = FIGURES[2][1]
        views["side_mirror"] = View("side_mirror", img[:, x0 - 20:x1 + 21], 90.0, mirror=True)
    if calibrated and os.path.exists(CALIB):
        import json
        cal = json.load(open(CALIB))
        for name, v in views.items():
            c = cal.get("side" if name == "side_mirror" else name)
            if not c:
                continue
            if name == "side_mirror":
                v.u0 = v.w - 1 - c["u0"]
            else:
                v.u0 = c["u0"]
                v.set_azimuth(c["az"])
    return views


def rasterize_mask(view, tris):
    """tris: (T,3,3) world triangles -> boolean mask in the view's pixel grid."""
    im = Image.new("L", (view.w, view.h), 0)
    dr = ImageDraw.Draw(im)
    uv = view.project(tris.reshape(-1, 3)).reshape(-1, 3, 2)
    for t in uv:
        dr.polygon([tuple(t[0]), tuple(t[1]), tuple(t[2])], fill=255)
    return np.array(im) > 127


def iou(a, b):
    return (a & b).sum() / max(1, (a | b).sum())


def overlay(view, model_mask):
    """Reference in grey, model silhouette outline in red, reference-only area in blue."""
    ref = view.mask
    out = np.full((view.h, view.w, 3), 255, np.uint8)
    rgb = view.rgba[:, :, :3].astype(float)
    a = view.rgba[:, :, 3:4] / 255.0
    base = (rgb * a + 255 * (1 - a)) * 0.55 + 255 * 0.45
    out[:] = base.astype(np.uint8)
    out[model_mask & ~ref] = (230, 60, 60)
    out[ref & ~model_mask] = (60, 90, 230)
    return out


# ---------------------------------------------------------------------------
# Pixel classes of the drawing (used for part-aware fitting and texture masks)
# ---------------------------------------------------------------------------
BG, HAIR, SKIN, SHIRT, PANTS, STRING, LINE = range(7)
CLASS_NAMES = ["bg", "hair", "skin", "shirt", "pants", "string", "line"]
PROTOS = [
    (HAIR, (70, 36, 25)), (HAIR, (113, 63, 43)), (HAIR, (150, 95, 62)),
    (SKIN, (246, 161, 109)), (SKIN, (194, 112, 75)), (SKIN, (250, 200, 170)),
    (SHIRT, (58, 59, 94)), (SHIRT, (40, 41, 68)),
    (PANTS, (139, 142, 170)), (PANTS, (125, 129, 159)), (PANTS, (106, 109, 140)),
    (PANTS, (175, 168, 183)), (PANTS, (86, 88, 118)),
    (STRING, (228, 218, 204)), (STRING, (200, 188, 176)),
    (LINE, (27, 16, 23)),
]


def classify(view, smooth=5):
    from scipy import ndimage
    rgb = view.rgba[:, :, :3].astype(float)
    P = np.array([p for _, p in PROTOS], float)
    lab = np.array([c for c, _ in PROTOS])
    d = ((rgb[:, :, None, :] - P[None, None]) ** 2).sum(-1)
    raw = lab[d.argmin(-1)]
    raw[~view.mask] = BG
    # majority vote in a small window; ink lines take their neighbours' class
    scores = []
    for c in range(LINE):
        ind = (raw == c).astype(float)
        scores.append(ndimage.uniform_filter(ind, smooth))
    lbl = np.argmax(np.stack(scores, -1), -1)
    lbl[~view.mask] = BG
    lbl[view.mask & (lbl == BG)] = SKIN
    # eyes, brows and lips are enclosed by skin: they belong to the face
    skin = lbl == SKIN
    closed = ndimage.binary_closing(skin, iterations=4)   # bridge lashes touching the hair
    holes = ndimage.binary_fill_holes(closed) & ~skin
    comp, n = ndimage.label(holes)
    if n:
        sizes = ndimage.sum(np.ones_like(comp), comp, range(1, n + 1))
        lbl[np.isin(comp, np.where(sizes < 2500)[0] + 1)] = SKIN
    # the face drawn over the modelled face region is face, whatever its colour (eyes, lashes)
    face = face_region(view)
    lbl[face & np.isin(lbl, (HAIR, SHIRT, PANTS, STRING))] = SKIN
    # spatial clean-up: plaid shadows vs sweater, highlights, plaid dots, lashes
    z = (view.floor - np.arange(view.h))[:, None] * view.s * np.ones((1, view.w))
    lbl[(lbl == SHIRT) & (z < 0.84)] = PANTS
    lbl[(lbl == PANTS) & (z > 1.02)] = SHIRT
    for c, keep, repl in ((STRING, 60, PANTS), (HAIR, 80, SKIN), (SHIRT, 60, PANTS), (PANTS, 60, SHIRT)):
        comp, n = ndimage.label(lbl == c)
        if n:
            sizes = ndimage.sum(np.ones_like(comp), comp, range(1, n + 1))
            small = np.isin(comp, np.where(sizes < keep)[0] + 1)
            lbl[small] = repl
    return lbl


_FACE_CACHE = {}


def face_region(view):
    """Pixels covered by the front of the modelled face (head_from_drawing) in this view."""
    key = (view.name, round(view.az, 3), round(view.u0, 3))
    if key in _FACE_CACHE:
        return _FACE_CACHE[key]
    import head_from_drawing as HD
    from scipy import ndimage
    pts, nrm = [], []
    for z in np.arange(1.440, 1.578, 0.002):
        for th in np.linspace(-1.05, 1.05, 90):
            x, y = HD.surface(th, z)
            x2, y2 = HD.surface(th + 0.01, z)
            x3, y3 = HD.surface(th, z + 0.002)
            n = np.cross([x2 - x, y2 - y, 0.0], [x3 - x, y3 - y, 0.002])
            n /= np.linalg.norm(n) + 1e-12
            if n[1] > 0:
                n = -n
            pts.append((x, y, z))
            nrm.append(n)
    P, N = np.array(pts), np.array(nrm)
    facing = N @ (-np.asarray(view.d))
    P = P[facing > 0.35]
    m = np.zeros((view.h, view.w), bool)
    if len(P):
        uv = np.round(view.project(P)).astype(int)
        ok = (uv[:, 0] >= 0) & (uv[:, 0] < view.w) & (uv[:, 1] >= 0) & (uv[:, 1] < view.h)
        m[uv[ok, 1], uv[ok, 0]] = True
        m = ndimage.binary_closing(m, iterations=2)
        m = ndimage.binary_erosion(m, iterations=2)
    _FACE_CACHE[key] = m
    return m
