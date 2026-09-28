"""Stylized female anatomy for the pajama character, in meters (Z up, facing -Y).

Proportions (H = one head, crown to chin = 0.232 m, measured on the sheet):

                         real woman        anime norm           this character (sheet)
    height (skull top)   7-7.5 H           6-7.5 H              7.15 H  (1.66 m)
    crotch               ~0.5 of height    same / a bit lower   0.80 m  (0.48)
    elbow                navel height      same                 1.06 m
    wrist                crotch height     often higher         0.86 m  (short, cute arms)
    shoulders            ~2 H wide         narrower             0.33 m (bone), 0.37 m with sweater
    hips                 ~= shoulders      wider than shoulders 0.34 m
    waist : hip          ~0.7              ~0.65-0.7            0.73
    neck                 ~11 cm wide       thin, ~8-9 cm        8.6 cm
    eyes                 ~3 cm, on the     large, low, set wide 5.2 cm each, eye line at
                         head's midline                         1.537 (just below mid-head)
    nose / mouth         full structure    tiny, a hint         tiny tip, short smile line

Every body part is a loft of superellipse cross-sections along a path. A section is
(half_width, front_depth, back_depth, roundness), so asymmetric shapes like the chest
(more in front), buttocks (more behind) or calf (bulge behind the shin) come out right.
"""
import math

import numpy as np
from mathutils import Vector

H = 0.232
SKULL_TOP = 1.660
CHIN = 1.428

# ---------------------------------------------------------------- landmarks
L = {
    "crotch": 0.795, "hip_joint": 0.855, "iliac": 0.935, "waist": 0.995, "navel": 0.985,
    "underbust": 1.135, "bust": 1.195, "armpit": 1.255, "clavicle": 1.325, "neck_base": 1.345,
    "knee": 0.465, "calf": 0.36, "ankle": 0.075,
    "shoulder_joint": 1.300, "elbow": 1.060, "wrist": 0.860,
}
SHOULDER_X = 0.150        # glenohumeral joint
HIP_X = 0.085             # femoral head (leg axis at the top)


def V(x, y, z):
    return Vector((x, y, z))


# --------------------------------------------------------------- torso table
# z, half width (X), front (−Y) depth, back (+Y) depth, roundness exponent, y centre offset
TORSO = [
    (0.790, 0.060, 0.050, 0.060, 2.2, 0.010),
    (0.820, 0.112, 0.070, 0.092, 2.3, 0.012),   # crotch -> upper thighs (overlaps legs)
    (0.855, 0.166, 0.080, 0.097, 2.6, 0.012),   # hips at the greater trochanter + glutes
    (0.900, 0.164, 0.078, 0.094, 2.6, 0.010),
    (0.935, 0.155, 0.074, 0.088, 2.5, 0.008),   # iliac crest
    (0.995, 0.124, 0.066, 0.074, 2.4, 0.006),   # waist
    (1.060, 0.130, 0.074, 0.076, 2.4, 0.004),   # lower ribs
    (1.135, 0.140, 0.088, 0.080, 2.4, 0.002),   # underbust
    (1.195, 0.148, 0.112, 0.082, 2.5, 0.000),   # bust (modest, soft)
    (1.255, 0.150, 0.092, 0.086, 2.6, 0.002),   # armpit
    (1.310, 0.148, 0.074, 0.080, 2.8, 0.006),
    (1.345, 0.125, 0.054, 0.066, 2.6, 0.010),   # clavicles / shoulder slope
    (1.368, 0.070, 0.044, 0.052, 2.2, 0.012),   # trapezius into the neck
]
NECK = [   # z, half width, front, back, roundness, y offset
    (1.345, 0.052, 0.046, 0.052, 2.1, 0.012),
    (1.385, 0.044, 0.042, 0.044, 2.0, 0.012),
    (1.420, 0.043, 0.042, 0.044, 2.0, 0.010),
    (1.470, 0.045, 0.044, 0.046, 2.0, 0.008),   # hidden inside the head
]


# ---------------------------------------------------------------- limb tables
# t along the limb (0 = top joint), half width (across), front depth, back depth, roundness
LEG = [
    (0.00, 0.040, 0.040, 0.045, 2.0),   # buried inside the pelvis
    (0.12, 0.080, 0.080, 0.090, 2.2),
    (0.20, 0.080, 0.080, 0.084, 2.2),   # upper thigh
    (0.34, 0.068, 0.070, 0.070, 2.1),
    (0.50, 0.055, 0.057, 0.055, 2.0),   # above the knee
    (0.555, 0.050, 0.052, 0.048, 2.2),  # knee
    (0.61, 0.047, 0.044, 0.052, 2.1),
    (0.68, 0.050, 0.040, 0.064, 2.0),   # calf bulge (behind)
    (0.80, 0.041, 0.036, 0.048, 2.0),
    (0.92, 0.030, 0.030, 0.034, 2.1),
    (1.00, 0.028, 0.031, 0.032, 2.2),   # ankle
]
ARM = [
    (0.00, 0.030, 0.030, 0.030, 2.0),   # buried in the shoulder
    (0.10, 0.043, 0.046, 0.044, 2.0),   # deltoid
    (0.20, 0.045, 0.044, 0.044, 2.0),
    (0.50, 0.037, 0.038, 0.038, 2.0),   # upper arm
    (0.57, 0.033, 0.034, 0.036, 2.0),   # elbow
    (0.66, 0.035, 0.036, 0.034, 2.1),   # forearm muscles near the elbow
    (0.90, 0.028, 0.022, 0.022, 2.2),
    (1.00, 0.026, 0.018, 0.018, 2.4),   # wrist (flatter)
]


def leg_path(sx):
    top = V(sx * 0.060, 0.012, 0.960)
    knee = V(sx * 0.098, 0.002, L["knee"])
    ankle = V(sx * 0.112, 0.024, L["ankle"])
    hip = V(sx * HIP_X, 0.012, L["hip_joint"])
    return [top, hip, hip.lerp(knee, 0.5) + V(sx * 0.004, -0.006, 0), knee, knee.lerp(ankle, 0.5) + V(0, 0.004, 0), ankle]


def arm_path(sx):
    inner = V(sx * 0.100, 0.012, L["shoulder_joint"] - 0.004)
    sh = V(sx * SHOULDER_X, 0.014, L["shoulder_joint"])
    el = V(sx * 0.188, 0.024, L["elbow"])
    wr = V(sx * 0.203, 0.006, L["wrist"])
    return [inner, sh, sh.lerp(el, 0.5) + V(sx * 0.004, 0, 0), el, el.lerp(wr, 0.5), wr]


def joints():
    """Bone head/tail positions for the rig (same bone names as before)."""
    j = {
        "hips": (V(0, 0.010, 0.880), V(0, 0.008, 0.990)),
        "spine": (V(0, 0.008, 0.990), V(0, 0.004, 1.140)),
        "chest": (V(0, 0.004, 1.140), V(0, 0.010, 1.330)),
        "neck": (V(0, 0.012, 1.345), V(0, 0.008, 1.455)),
        "head": (V(0, 0.008, 1.455), V(0, 0.000, 1.650)),
    }
    for sx, s in ((1, "L"), (-1, "R")):
        a = arm_path(sx)
        lg = leg_path(sx)
        hand_tip = a[-1] + V(sx * 0.004, -0.004, -0.150)
        foot = V(sx * 0.118, -0.085, 0.022)
        toe = V(sx * 0.126, -0.165, 0.014)
        j.update({
            f"shoulder.{s}": (V(sx * 0.025, 0.012, 1.325), a[1]),
            f"upper_arm.{s}": (a[1], a[3]),
            f"forearm.{s}": (a[3], a[5]),
            f"hand.{s}": (a[5], hand_tip),
            f"thigh.{s}": (lg[1], lg[3]),
            f"shin.{s}": (lg[3], lg[5]),
            f"foot.{s}": (lg[5], foot),
            f"toe.{s}": (foot, toe),
        })
    return j


PARENT = {"hips": None, "spine": "hips", "chest": "spine", "neck": "chest", "head": "neck"}
for _s in "LR":
    PARENT.update({f"shoulder.{_s}": "chest", f"upper_arm.{_s}": f"shoulder.{_s}",
                   f"forearm.{_s}": f"upper_arm.{_s}", f"hand.{_s}": f"forearm.{_s}",
                   f"thigh.{_s}": "hips", f"shin.{_s}": f"thigh.{_s}", f"foot.{_s}": f"shin.{_s}",
                   f"toe.{_s}": f"foot.{_s}"})
BONE_ORDER = ["hips", "spine", "chest", "neck", "head"] + [f"{b}.{s}" for s in "LR" for b in
                                                          ("shoulder", "upper_arm", "forearm", "hand",
                                                           "thigh", "shin", "foot", "toe")]


# ------------------------------------------------------------------ geometry
def _interp(table, t, col):
    from scipy.interpolate import PchipInterpolator
    ts = [r[0] for r in table]
    vs = [r[col] for r in table]
    return float(PchipInterpolator(ts, vs)(min(max(t, ts[0]), ts[-1])))


def _spline(pts, t):
    """Catmull-Rom through pts, t in 0..1 (arc-length-ish by segment)."""
    P = [pts[0] * 2 - pts[1]] + list(pts) + [pts[-1] * 2 - pts[-2]]
    n = len(pts) - 1
    u = min(max(t, 0.0), 1.0) * n
    i = min(int(u), n - 1)
    s = u - i
    p0, p1, p2, p3 = P[i], P[i + 1], P[i + 2], P[i + 3]
    return 0.5 * ((2 * p1) + (-p0 + p2) * s + (2 * p0 - 5 * p1 + 4 * p2 - p3) * s * s
                  + (-p0 + 3 * p1 - 3 * p2 + p3) * s * s * s)


def superellipse(a, front, back, n, k, m):
    """Point k of m around a section: x across, y depth (negative = front)."""
    ang = 2 * math.pi * k / m
    c, s = math.cos(ang), math.sin(ang)
    x = a * math.copysign(abs(c) ** (2 / n), c)
    d = back if s > 0 else front
    y = d * math.copysign(abs(s) ** (2 / n), s)
    return x, y


def loft(frames, m=40):
    """frames: list of (centre, x_axis, y_axis, a, front, back, n). Returns verts, faces (capped)."""
    verts, faces = [], []
    for (c, xa, ya, a, fr, bk, n) in frames:
        for k in range(m):
            x, y = superellipse(a, fr, bk, n, k, m)
            verts.append(tuple(c + xa * x + ya * y))
    rings = len(frames)
    for j in range(rings - 1):
        for k in range(m):
            k2 = (k + 1) % m
            faces.append((j * m + k, j * m + k2, (j + 1) * m + k2, (j + 1) * m + k))
    for j, flip in ((0, True), (rings - 1, False)):
        verts.append(tuple(frames[j][0]))
        ci = len(verts) - 1
        for k in range(m):
            k2 = (k + 1) % m
            f = (j * m + k, j * m + k2, ci)
            faces.append(tuple(reversed(f)) if flip else f)
    return verts, faces


def vertical_loft(table, rings=90, m=48):
    z0, z1 = table[0][0], table[-1][0]
    frames = []
    for i in range(rings):
        z = z0 + (z1 - z0) * i / (rings - 1)
        frames.append((V(0, _interp(table, z, 5), z), V(1, 0, 0), V(0, 1, 0),
                       _interp(table, z, 1), _interp(table, z, 2), _interp(table, z, 3), _interp(table, z, 4)))
    return loft(frames, m)


def limb_loft(path, table, sx, rings=70, m=36, flip_depth=False):
    frames = []
    for i in range(rings):
        t = i / (rings - 1)
        c = _spline(path, t)
        tan = (_spline(path, min(1, t + 0.01)) - _spline(path, max(0, t - 0.01))).normalized()
        ya = Vector((0, 1, 0))
        ya = (ya - tan * ya.dot(tan)).normalized()          # depth axis: world +Y (back)
        xa = ya.cross(tan).normalized() * (1 if sx > 0 else -1)
        fr, bk = _interp(table, t, 2), _interp(table, t, 3)
        if flip_depth:
            fr, bk = bk, fr
        frames.append((c, xa, ya, _interp(table, t, 1), fr, bk, _interp(table, t, 4)))
    return loft(frames, m)


def foot(sx, m=32):
    """Foot: a loft from heel to ball (y is world Y: + back, - toes), plus five toes."""
    ank = leg_path(sx)[-1]
    yaw = -sx * 0.10                                   # toes turned slightly outward
    cy, syw = math.cos(yaw), math.sin(yaw)

    def P(x, y, z):
        return V(ank.x + sx * x * cy - y * syw, ank.y + y * cy + sx * x * syw, z)

    # t, x(out), y, z centre, half width, bottom depth, top height, roundness
    prof = [(0.00, 0.000, 0.000, 0.085, 0.028, 0.030, 0.030, 2.1),   # up inside the ankle
            (0.08, 0.000, 0.040, 0.045, 0.028, 0.034, 0.030, 2.2),
            (0.18, 0.000, 0.022, 0.040, 0.034, 0.036, 0.032, 2.3),
            (0.45, 0.004, -0.040, 0.034, 0.040, 0.028, 0.026, 2.6),
            (0.80, 0.009, -0.110, 0.021, 0.046, 0.016, 0.016, 3.0),
            (1.00, 0.010, -0.135, 0.017, 0.044, 0.012, 0.011, 3.0)]
    frames = []
    for i in range(40):
        t = i / 39
        _, x, y, z, a, bot, top, n = [np.interp(t, [p[0] for p in prof], [p[c] for p in prof]) for c in range(8)]
        frames.append((P(x, y, z), V(1, 0, 0), V(0, 0, 1), a, bot, top, n))
    parts = [loft(frames, m)]
    toes = [(-0.025, 0.036, 0.0118), (-0.007, 0.031, 0.0096), (0.008, 0.028, 0.0090),
            (0.021, 0.025, 0.0084), (0.033, 0.021, 0.0078)]
    for tx, ln, r in toes:
        frames = []
        for i in range(12):
            t = i / 11
            rr = r * (1.0 - 0.25 * t ** 2)
            frames.append((P(tx * (1 + 0.1 * t), -0.125 - ln * t, 0.017 - 0.004 * t), V(1, 0, 0), V(0, 0, 1),
                           rr, rr * 0.85, rr * 0.9, 2.2))
        parts.append(loft(frames, 16))
    return parts


def hand(sx, m=20):
    """Relaxed hand at the side: palm facing the thigh, fingers gently curled toward the palm."""
    wr = arm_path(sx)[-1]
    parts = []
    frames = []
    for i in range(16):
        t = i / 15
        c = wr + V(sx * 0.002 * t, -0.002 * t, -0.085 * t)
        a = 0.022 + 0.014 * math.sin(math.pi * min(1, t * 0.9)) ** 0.6   # front-back width of the hand
        frames.append((c, V(0, -1, 0), V(sx, 0, 0), a, 0.011 + 0.002 * t, 0.012, 2.4))
    parts.append(loft(frames, m))
    knuckle_z = wr.z - 0.085
    fingers = [(-0.018, 0.070, 0.0080), (-0.006, 0.078, 0.0082), (0.006, 0.074, 0.0078), (0.017, 0.060, 0.0070)]
    for fy, ln, r in fingers:
        base = V(wr.x + sx * 0.003, wr.y + fy, knuckle_z + 0.008)
        frames = []
        for i in range(18):
            t = i / 17
            p = base + V(-sx * 0.016 * t ** 2, 0, -ln * t)
            rr = r * (1 - 0.22 * t)
            frames.append((p, V(0, -1, 0), V(sx, 0, 0), rr, rr * 0.9, rr * 0.9, 2.2))
        parts.append(loft(frames, 14))
    base = wr + V(-sx * 0.006, -0.020, -0.030)
    frames = []
    for i in range(14):
        t = i / 13
        p = base + V(-sx * 0.010 * t, -0.012 * t, -0.050 * t)
        rr = 0.0105 * (1 - 0.25 * t)
        frames.append((p, V(0, -1, 0), V(sx, 0, 0), rr, rr * 0.9, rr * 0.9, 2.2))
    parts.append(loft(frames, 14))
    return parts


# where separately lofted parts meet: soft weights for blending after the union
def junction_weight(p):
    w = 0.0
    # pelvis / thigh transition: a band around the whole hip at crotch height
    w = max(w, max(0.0, 1 - abs(p.z - 0.850) / 0.095) * (1.0 if abs(p.x) < 0.20 else 0.0))
    # neck base
    w = max(w, max(0.0, 1 - (p - V(0, 0.012, 1.358)).length / 0.07))
    for sx in (1, -1):
        w = max(w, max(0.0, 1 - (p - V(sx * 0.130, 0.012, 1.300)).length / 0.075))   # shoulder / armpit
        w = max(w, max(0.0, 1 - (p - V(sx * 0.112, 0.024, 0.070)).length / 0.065))   # ankle
        w = max(w, max(0.0, 1 - (p - V(sx * 0.203, 0.006, 0.860)).length / 0.030))  # wrist
    return w ** 1.2
