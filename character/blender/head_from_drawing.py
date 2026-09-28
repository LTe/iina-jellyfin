"""Head modelled from the drawing's front outline and side profile.

Every horizontal slice of the head is a superellipse whose half-width comes from
the drawn face outline (front view) and whose front depth comes from the drawn
profile (side view). The profile is split into a smooth face plane plus features
(nose, lips, chin) that only protrude near the centre line, each with its own
sideways falloff, so the nose is a nose rather than a ridge around the head.
All numbers are in world metres and were traced from reference/turnaround.png
(see headref.py).
"""
import math

import numpy as np

XC = 0.018          # the drawn face sits ~2 cm right of the body's centre line
Z_TOP = 1.660

HALF_WIDTH = [(1.422, 0.022), (1.430, 0.031), (1.436, 0.038), (1.448, 0.051), (1.460, 0.061), (1.472, 0.067), (1.484, 0.071),
              (1.500, 0.074), (1.516, 0.077), (1.532, 0.079), (1.548, 0.080), (1.566, 0.080), (1.585, 0.078),
              (1.600, 0.075)]
FACE_PLANE = [(1.422, -0.080), (1.436, -0.093), (1.448, -0.096), (1.460, -0.099), (1.472, -0.102), (1.484, -0.104),
              (1.500, -0.106), (1.516, -0.107), (1.532, -0.109), (1.548, -0.114), (1.560, -0.120), (1.575, -0.123),
              (1.590, -0.123), (1.600, -0.121)]
PROFILE = [(1.422, -0.084), (1.430, -0.093), (1.440, -0.097), (1.450, -0.099), (1.458, -0.104), (1.465, -0.109),
           (1.471, -0.108), (1.478, -0.113), (1.486, -0.115), (1.492, -0.122), (1.498, -0.129), (1.505, -0.127),
           (1.513, -0.120), (1.522, -0.114), (1.532, -0.112), (1.544, -0.114), (1.556, -0.121), (1.575, -0.123)]
BACK = [(1.440, 0.050), (1.460, 0.078), (1.500, 0.098), (1.550, 0.106), (1.600, 0.100)]
# sideways falloff (radians) of profile features by height: chin, lips, nose
SIGMA = [(1.420, 0.55), (1.450, 0.45), (1.458, 0.32), (1.484, 0.30), (1.490, 0.17), (1.528, 0.15), (1.545, 0.9)]


_CACHE = {}


def interp(table, z):
    """Smooth (monotone cubic) interpolation of a traced table."""
    from scipy.interpolate import PchipInterpolator
    key = id(table)
    if key not in _CACHE:
        zs, vs = zip(*table)
        _CACHE[key] = (PchipInterpolator(zs, vs, extrapolate=True), zs[0], zs[-1])
    f, lo, hi = _CACHE[key]
    return float(f(min(max(z, lo), hi)))


DOME_START = 1.582


def dome(z, start=DOME_START, top=Z_TOP):
    """Round off the skull above `start`."""
    if z <= start:
        return 1.0
    t = min(1.0, (z - start) / (top - start))
    return math.sqrt(max(0.0, 1 - t * t))


def jaw_z(theta):
    """Lowest point of the head for an azimuth (0 = front): chin, jaw corner, skull base."""
    a = min(1.0, abs(theta) / math.pi * 1.5)
    s = a * a * (3 - 2 * a)
    return 1.422 + (1.458 - 1.422) * s - 0.03 * max(0.0, abs(theta) / math.pi - 0.6) ** 2


def surface(theta, z_in):
    z = min(z_in, DOME_START)          # the skull above the forehead is a smooth dome
    w = interp(HALF_WIDTH, z)
    front = interp(FACE_PLANE, z)
    prof = interp(PROFILE, z) if z <= PROFILE[-1][0] else front
    feat = max(0.0, front - prof)                        # how far the profile sticks out of the face plane
    sig = interp(SIGMA, z)
    back = interp(BACK, z)
    k = dome(z_in)
    z = z_in
    w, back = w * k, back * k
    front = front * k
    feat = feat * k
    c, s = math.cos(theta), math.sin(theta)
    n_front, n_back = 2.5, 2.1
    if c >= 0:
        d = -front + feat * math.exp(-(theta / sig) ** 2)
        y = -d * abs(c) ** (2 / n_front)
        x = w * math.copysign(abs(s) ** (2 / n_front), s)
    else:
        y = back * abs(c) ** (2 / n_back)
        x = w * math.copysign(abs(s) ** (2 / n_back), s)
    # eye sockets and cheekbones
    for sx in (-1, 1):
        dx, dz = (x - sx * 0.036) / 0.016, (z - 1.535) / 0.012
        if c > 0:
            y += 0.004 * math.exp(-(dx * dx + dz * dz))
        dx, dz = (x - sx * 0.058) / 0.02, (z - 1.505) / 0.016
        if c > 0:
            y -= 0.003 * math.exp(-(dx * dx + dz * dz))
    return XC + x, y


def build_mesh(n_theta=128, n_rows=80, n_under=6):
    verts, faces = [], []
    thetas = [-math.pi + 2 * math.pi * i / n_theta for i in range(n_theta)]
    rows = []
    for j in range(n_rows + 1):
        t = j / n_rows
        row = []
        for th in thetas:
            zb = jaw_z(th)
            # denser rows on the face: ease the parameter
            z = Z_TOP - (Z_TOP - zb) * (1 - math.cos(t * math.pi / 2) ** 1.3)
            if j == 0:
                row.append(len(verts))
                verts.append((XC, 0.010, Z_TOP))
                continue
            x, y = surface(th, z)
            row.append(len(verts))
            verts.append((x, y, z))
        rows.append(row)
    # under the jaw: curl the lowest ring in toward the neck (hidden inside it)
    last = [verts[i] for i in rows[-1]]
    for k in range(1, n_under + 1):
        f = k / n_under
        row = []
        for (x, y, z) in last:
            cx, cy, cz = XC * 0.5, 0.020, 1.425
            row.append(len(verts))
            verts.append((x + (cx - x) * f * 0.85, y + (cy - y) * f * 0.85, z - 0.004 * f + (cz - z) * f * 0.3))
        rows.append(row)
    for j in range(len(rows) - 1):
        a, b = rows[j], rows[j + 1]
        for i in range(n_theta):
            i2 = (i + 1) % n_theta
            if j == 0:
                faces.append((a[0], b[i2], b[i]))
            else:
                faces.append((a[i], a[i2], b[i2], b[i]))
    cap = len(verts)
    verts.append((XC * 0.5, 0.020, 1.418))
    for i in range(n_theta):
        faces.append((rows[-1][i], rows[-1][(i + 1) % n_theta], cap))
    return smooth(verts, faces, iters=2), faces


def smooth(verts, faces, iters=2, f=0.35):
    V = np.array(verts, float)
    nb = [set() for _ in V]
    for fc in faces:
        for i in range(len(fc)):
            a, b = fc[i], fc[(i + 1) % len(fc)]
            nb[a].add(b)
            nb[b].add(a)
    for _ in range(iters):
        avg = np.array([V[list(n)].mean(0) if n else V[i] for i, n in enumerate(nb)])
        V = V + (avg - V) * f
    return [tuple(v) for v in V]


def ear_frame(sx):
    """Centre, size and yaw of each ear, from the side view."""
    w = interp(HALF_WIDTH, 1.527)
    return (XC + sx * (w + 0.002), 0.020, 1.527), (0.010, 0.019, 0.033), -sx * 0.30
