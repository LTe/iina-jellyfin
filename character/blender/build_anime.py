"""Anime-style build of the pajama character (the standard stylized-character pipeline).

    python3 character/blender/build_anime.py            # build, export, render
    python3 character/blender/build_anime.py --no-render

Pipeline, following how anime / toon game characters are usually made:
  * body and clothes: the rigged base body and separate garment meshes from
    build_character.py, fitted to the sheet's outlines and then relaxed so the
    surfaces stay clean;
  * head: modelled from the drawn face outline and profile (head_from_drawing.py);
  * face: ONE flat painted decal (eyes, brows, lashes, mouth, blush) projected
    from the front only, drawn as vector shapes at the positions measured on the
    sheet, so there is exactly one set of features;
  * hair: its own object, built like curve hair: a continuous hair volume over
    the scalp plus tapered, lens-profiled strand clumps that each run unbroken
    from the hairline to the bun, a bun wound from thick clumps, and loose locks;
  * shading: flat albedo + cel shading (Shader to RGB -> constant ramp) in
    EEVEE, inverted-hull ink outlines. The exported glTF keeps plain PBR
    materials (base colours / textures) so any engine can apply its own toon shader.
"""
import math
import os
import random
import sys

import bpy
import bmesh
import numpy as np
from mathutils import Vector, Matrix
from mathutils.bvhtree import BVHTree

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import build_character as BC  # noqa: E402
import head_from_drawing as HD  # noqa: E402

V = BC.V
RNG = random.Random(11)
ROOT = BC.ROOT
OUT_EXPORT, OUT_RENDER, OUT_TEX = BC.OUT_EXPORT, BC.OUT_RENDER, BC.OUT_TEX

XC = HD.XC                      # the face's centre line in the drawing
HC = V(XC, 0.004, 1.535)        # head centre used for hair directions
BUN = V(-0.012, 0.086, 1.690)   # measured from the front, side and back views
BUN_R = 0.070

PAL = {
    "skin": "#F3A877", "skin_shade": "#D98763",
    "hair": "#6B3A21", "hair_shade": "#43231A", "hair_shade_rel": "#9A8580", "hair_light": "#7A4529", "hair_dark": "#5C311D",
    "sweater": "#343A66", "sweater_shade": "#23284A",
    "plaid": "#8C93BD", "string": "#EDE3D3",
    "tee": "#CFC8E2", "under": "#D8CFC6",
    "ink": "#2A1A1C",
}


# ---------------------------------------------------------------------------
# Face decal: vector-drawn at the positions measured on the sheet
# ---------------------------------------------------------------------------
FACE_U = (XC - 0.12, XC + 0.12)
FACE_V = (1.40, 1.64)


def face_uv(x, z):
    return (x - FACE_U[0]) / (FACE_U[1] - FACE_U[0]), (z - FACE_V[0]) / (FACE_V[1] - FACE_V[0])


def face_texture(size=2048, ss=2):
    from PIL import Image, ImageDraw, ImageFilter
    S = size * ss
    skin = BC.rgb255(PAL["skin"])
    im = Image.new("RGBA", (S, S), skin + (255,))

    def P(x, z):
        u, v = face_uv(XC + x, z)
        return (u * S, (1 - v) * S)

    def M(m):
        return m / (FACE_U[1] - FACE_U[0]) * S

    def layer(draw_fn, blur=0.0):
        L = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        draw_fn(ImageDraw.Draw(L))
        if blur:
            L = L.filter(ImageFilter.GaussianBlur(M(blur)))
        im.alpha_composite(L)

    # blush + freckles
    def blush(d):
        for sx in (-1, 1):
            cx, cz = sx * 0.050, 1.505
            d.ellipse([P(cx - 0.021, cz + 0.012), P(cx + 0.021, cz - 0.012)], fill=(240, 120, 100, 95))
    layer(blush, blur=0.007)

    def freckles(d):
        r = random.Random(3)
        for sx in (-1, 1):
            for _ in range(6):
                x, z = sx * r.uniform(0.035, 0.065), r.uniform(1.498, 1.514)
                q = M(0.0009)
                c = P(x, z)
                d.ellipse([c[0] - q, c[1] - q, c[0] + q, c[1] + q], fill=(196, 110, 80, 150))
    layer(freckles)

    lash = (40, 22, 18, 255)
    for sx in (-1, 1):
        cx, cz = sx * 0.042, 1.536
        inner, outer = cx - sx * 0.024, cx + sx * 0.028

        def lid(t, top):
            x = inner + (outer - inner) * t
            if top:   # rounder upper lid, peak slightly to the outside
                z = cz + 0.002 + 0.0165 * math.sin(math.pi * t ** 0.9) + 0.003 * t
            else:
                z = cz - 0.004 - 0.0105 * math.sin(math.pi * t) ** 0.8 + 0.004 * t
            return x, z

        up = [lid(i / 40, True) for i in range(41)]
        lo = [lid(i / 40, False) for i in range(41)]
        shape = [P(*q) for q in up] + [P(*q) for q in reversed(lo)]
        eye = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        ed = ImageDraw.Draw(eye)
        ed.polygon(shape, fill=(252, 247, 242, 255))
        ix, iz = cx + sx * 0.002, cz - 0.001
        r = 0.0158
        # iris: dark rim, warm gradient toward the bottom, pupil, highlights
        ed.ellipse([P(ix - r, iz + r * 1.05), P(ix + r, iz - r * 1.05)], fill=(70, 36, 20, 255))
        for k in range(10):
            rr = r * (0.9 - 0.035 * k)
            off = -0.0045 * k / 10
            c = (int(95 + 10 * k), int(52 + 6 * k), int(26 + 3 * k), 255)
            ed.ellipse([P(ix - rr, iz + rr + off), P(ix + rr, iz - rr + off)], fill=c)
        rp = 0.0068
        ed.ellipse([P(ix - rp, iz + rp + 0.001), P(ix + rp, iz - rp + 0.001)], fill=(30, 15, 10, 255))
        # upper lid shadow on the eyeball
        ed.polygon([P(*q) for q in up] + [P(q[0], q[1] - 0.006) for q in reversed(up)], fill=(90, 60, 50, 90))
        h = 0.0042
        hx, hz = ix + sx * 0.005, iz + 0.006
        ed.ellipse([P(hx - h, hz + h), P(hx + h, hz - h)], fill=(255, 255, 255, 255))
        h2 = 0.0018
        hx, hz = ix - sx * 0.006, iz - 0.007
        ed.ellipse([P(hx - h2, hz + h2), P(hx + h2, hz - h2)], fill=(255, 245, 235, 230))
        mask = Image.new("L", (S, S), 0)
        ImageDraw.Draw(mask).polygon(shape, fill=255)
        a = eye.getchannel("A")
        eye.putalpha(Image.composite(a, Image.new("L", (S, S), 0), mask))
        im.alpha_composite(eye)
        d = ImageDraw.Draw(im)
        # upper lash line: thick, tapering, with a flick at the outer corner
        pts = [P(*q) for q in up]
        for i in range(len(pts) - 1):
            t = i / (len(pts) - 1)
            w = M(0.0012 + 0.0034 * math.sin(math.pi * min(1.0, 0.15 + t * 0.95)) ** 0.6)
            d.line([pts[i], pts[i + 1]], fill=lash, width=max(1, int(w)))
            d.ellipse([pts[i][0] - w / 2, pts[i][1] - w / 2, pts[i][0] + w / 2, pts[i][1] + w / 2], fill=lash)
        ox, oz = up[-1]
        for k, (dx, dz, w) in enumerate(((0.008, 0.004, 0.0026), (0.006, 0.0065, 0.0016))):
            d.line([P(ox - sx * 0.002, oz - 0.001), P(ox + sx * dx, oz + dz)], fill=lash, width=int(M(w)))
        d.line([P(*q) for q in lo[6:37]], fill=(150, 92, 72, 220), width=int(M(0.0009)))
        # double-lid crease
        d.line([P(inner + (outer - inner) * t, cz + 0.024 + 0.004 * math.sin(math.pi * t))
                for t in [i / 16 for i in range(2, 15)]], fill=BC.rgb255(PAL["skin_shade"]) + (255,),
               width=int(M(0.0011)))
        # eyebrow: thick at the inside, tapering to the outside, gentle arch
        for i in range(41):
            t = i / 40
            x = cx - sx * 0.024 + sx * 0.052 * t
            z = 1.568 + 0.009 * math.sin(math.pi * (0.15 + 0.8 * t)) - 0.003 * t
            w = M(0.0055 * (1 - 0.7 * t) + 0.0014)
            p = P(x, z)
            d.ellipse([p[0] - w / 2, p[1] - w / 2, p[0] + w / 2, p[1] + w / 2], fill=(70, 40, 26, 255))

    d = ImageDraw.Draw(im)
    # nose: a small shadow under the tip, one nostril hint each side
    layer(lambda dd: dd.ellipse([P(-0.010, 1.492), P(0.010, 1.484)], fill=BC.rgb255(PAL["skin_shade"]) + (130,)),
          blur=0.002)
    for sx in (-1, 1):
        c = P(sx * 0.0065, 1.4875)
        q = M(0.0016)
        d.ellipse([c[0] - q, c[1] - q * 0.7, c[0] + q, c[1] + q * 0.7], fill=(170, 95, 70, 255))
    # mouth: closed smile + soft lower lip
    layer(lambda dd: dd.ellipse([P(-0.016, 1.469), P(0.016, 1.458)], fill=(222, 128, 112, 150)), blur=0.002)
    mp = [P(-0.030 + 0.060 * t, 1.4725 - 0.0055 * math.sin(math.pi * t) + 0.004 * (abs(t - 0.5) * 2) ** 2.2)
          for t in [i / 30 for i in range(31)]]
    d.line(mp, fill=(128, 58, 50, 255), width=int(M(0.0017)), joint="curve")
    im = im.resize((size, size), Image.LANCZOS).convert("RGB")
    path = os.path.join(OUT_TEX, "anime_face.png")
    im.save(path)
    img = bpy.data.images.load(path)
    img.name = "anime_face"
    img.pack()
    return img


def strand_texture(size=512):
    """Anime hair strand shading: darker clump edges, soft root shadow, a highlight band."""
    from PIL import Image
    u = (np.arange(size) + 0.5) / size
    v = (np.arange(size) + 0.5) / size
    U, Vv = np.meshgrid(u, v)          # rows = v (along the strand)
    edge = np.abs(np.cos(2 * np.pi * U)) ** 4          # the two thin edges of the flat ribbon
    k = 1.0 - 0.38 * edge
    k *= 0.80 + 0.20 * np.clip(Vv / 0.25, 0, 1)        # root shadow
    band = np.exp(-((Vv - 0.36 - 0.02 * np.sin(U * 40)) / 0.035) ** 2) * (1 - edge)
    base = np.array(BC.rgb255(PAL["hair"]), float)
    hi = np.array(BC.rgb255("#B07A55"), float)
    img = base[None, None] * k[..., None]
    img = img * (1 - 0.55 * band[..., None]) + hi[None, None] * 0.55 * band[..., None]
    im = Image.fromarray(np.clip(img, 0, 255).astype(np.uint8)[::-1], "RGB")
    path = os.path.join(OUT_TEX, "hair_strand.png")
    im.save(path)
    img = bpy.data.images.load(path)
    img.name = "hair_strand"
    img.pack()
    return img


def face_uvs(head):
    """Front planar projection for the face; the back of the head maps onto plain skin."""
    me = head.data
    uv = me.uv_layers.new(name="UVMap")
    for loop in me.loops:
        co = me.vertices[loop.vertex_index].co
        u, v = face_uv(co.x, co.z)
        if co.y > 0.0:
            u, v = 0.01, 0.01
        uv.data[loop.index].uv = (min(max(u, 0.004), 0.996), min(max(v, 0.004), 0.996))


def smooth_face_normals(head):
    """Anime faces get their normals from a simple smooth proxy so cel shadows fall in clean
    shapes instead of following every bump of the nose and lips."""
    bm = bmesh.new()
    bmesh.ops.create_uvsphere(bm, u_segments=64, v_segments=48, radius=1.0)
    for v in bm.verts:
        v.co = Vector((XC + v.co.x * 0.080, 0.000 + v.co.y * 0.105, 1.535 + v.co.z * 0.115))
    me = bpy.data.meshes.new("FaceProxy")
    bm.to_mesh(me)
    bm.free()
    proxy = BC.link(bpy.data.objects.new("FaceProxy", me))
    BC.shade_smooth(proxy)
    dt = head.modifiers.new("FaceNormals", "DATA_TRANSFER")
    dt.object = proxy
    dt.use_loop_data = True
    dt.data_types_loops = {"CUSTOM_NORMAL"}
    dt.loop_mapping = "POLYINTERP_NEAREST"
    dt.mix_factor = 0.85
    try:
        BC.bake(head)
    except Exception as ex:   # keep going with plain normals
        print("face normal transfer skipped:", ex)
        head.modifiers.clear()
    bpy.data.objects.remove(proxy)


# ---------------------------------------------------------------------------
# Hair
# ---------------------------------------------------------------------------
HAIRLINE = [(0.0, 1.622), (0.45, 1.612), (0.90, 1.582), (1.15, 1.540), (1.30, 1.548), (1.55, 1.575),
            (1.80, 1.535), (2.15, 1.488), (2.60, 1.452), (math.pi, 1.446)]


def hairline_z(theta):
    a = abs(theta)
    zs = [z for _, z in HAIRLINE]
    ts = [t for t, _ in HAIRLINE]
    return float(np.interp(a, ts, zs))


def azimuth(d):
    """0 = straight ahead (-Y), +/-pi = back."""
    return math.atan2(d.x, -d.y)


def g(x, s):
    return math.exp(-(x / s) ** 2)


def hair_offset(d):
    """How far the hair volume stands off the scalp in direction d (from HC)."""
    th = abs(azimuth(d))
    z = HC.z + d.z * 0.12
    off = 0.012
    off += 0.012 * max(0.0, d.z)                                  # a little lift on top
    off += 0.048 * g(th - 1.55, 0.70) * g(z - 1.600, 0.070)       # messy volume at the sides
    off += 0.024 * g(th - math.pi, 0.9) * g(z - 1.535, 0.075)     # fullness at the back
    off -= 0.006 * g(th, 0.5) * g(z - 1.61, 0.02)                 # tight at the front hairline
    return off


class Surface:
    """Nearest-hit queries along rays from the head centre."""

    def __init__(self, ob):
        me = ob.data
        self.bvh = BVHTree.FromPolygons([v.co.copy() for v in me.vertices], [tuple(p.vertices) for p in me.polygons])

    def hit(self, d):
        loc, nrm, _, _ = self.bvh.ray_cast(HC + d * 0.4, -d)
        if loc is None:
            return HC + d * 0.1, d.copy()
        return loc, nrm


def build_hair_volume(head, mat, n_theta=160, n_rows=36):
    """One smooth, continuous hair mass over the scalp. Built as a grid whose bottom row
    lies exactly on the hairline, so the edge is a clean curve rather than a jagged cut."""
    scalp = Surface(head)

    def direction(th, e):
        return Vector((math.sin(th) * math.cos(e), -math.cos(th) * math.cos(e), math.sin(e)))

    def edge_elev(th):
        zt = hairline_z(th)
        lo, hi = -0.9, 1.5
        for _ in range(30):
            mid = (lo + hi) / 2
            p, _ = scalp.hit(direction(th, mid))
            if p.z < zt:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2

    verts, faces = [], []
    thetas = [-math.pi + 2 * math.pi * i / n_theta for i in range(n_theta)]
    edges = [edge_elev(th) for th in thetas]
    # light smoothing of the edge elevation around the head
    edges = [(edges[i - 1] + 2 * edges[i] + edges[(i + 1) % n_theta]) / 4 for i in range(n_theta)]
    for j in range(n_rows):
        t = j / n_rows
        for i, th in enumerate(thetas):
            e = edges[i] + (math.pi / 2 - edges[i]) * (t ** 0.9)
            d = direction(th, e)
            p, _ = scalp.hit(d)
            # the hair grows out of the skin at the edge and reaches full volume ~2 cm in
            k = min(1.0, t * 5.0) ** 0.7
            verts.append(tuple(p + d * (0.0015 + (hair_offset(d) - 0.0015) * k)))
    top_p, _ = scalp.hit(Vector((0, 0, 1)))
    verts.append(tuple(top_p + Vector((0, 0, 1)) * hair_offset(Vector((0, 0, 1)))))
    top = len(verts) - 1
    for j in range(n_rows - 1):
        for i in range(n_theta):
            a0, a1 = j * n_theta + i, j * n_theta + (i + 1) % n_theta
            faces.append((a0, a1, a1 + n_theta, a0 + n_theta))
    last = (n_rows - 1) * n_theta
    for i in range(n_theta):
        faces.append((last + i, last + (i + 1) % n_theta, top))
    ob = BC.mesh_obj("HairVolume", verts, faces, mat)
    sm = ob.modifiers.new("Smooth", "SMOOTH")
    sm.factor = 0.5
    sm.iterations = 3
    BC.bake(ob)
    bm = bmesh.new()
    bm.from_mesh(ob.data)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    bm.to_mesh(ob.data)
    bm.free()
    BC.shade_smooth(ob)
    ob.data.materials.clear()
    ob.data.materials.append(mat)
    uvl = ob.data.uv_layers.new(name="UVMap")
    for li in range(len(ob.data.loops)):
        uvl.data[li].uv = (0.0, 0.6)
    return ob


class UVBatch(BC.GeoBatch):
    def __init__(self):
        super().__init__()
        self.uv = []

    def build(self, name, mat):
        ob = super().build(name, mat)
        uvl = ob.data.uv_layers.new(name="UVMap")
        for poly in ob.data.polygons:
            for li in poly.loop_indices:
                uvl.data[li].uv = self.uv[ob.data.loops[li].vertex_index]
        return ob


def ribbon(batch, pts, width, thick, normal_fn, ring=8):
    v, f = BC.sweep_geo(pts, width, thick, up=normal_fn, ring=ring)
    n = len(pts)
    uv = [((k % ring) / ring, (k // ring) / max(1, n - 1)) for k in range(n * ring)]
    uv += [(0.25, 0.0), (0.25, 1.0)][: len(v) - n * ring]
    batch.add(v, f)
    if hasattr(batch, "uv"):
        batch.uv.extend(uv)


def taper(w0, root=0.35, tip=0.05, power=0.8):
    """Lens-shaped clump width along its length: narrow root, full body, pointed tip."""
    def fn(t):
        a = min(1.0, t / 0.18) if root < 1 else 1.0
        b = (1 - t) ** power
        return w0 * (root + (1 - root) * a) * (tip + (1 - tip) * b if t > 0.35 else 1.0)
    return fn


def build_hair_clumps(volume, head, mats):
    """Strand clumps, each one continuous from hairline to bun, lying on the hair volume."""
    surf = Surface(volume)
    base = UVBatch()
    light = UVBatch()
    dark = UVBatch()
    # where the swept-up hair gathers: a ring on the underside/front of the bun
    bun_up = (BUN - HC).normalized()

    def gather(th):
        side = Vector((math.cos(th), 0.0, 0.0))
        return BUN - bun_up * BUN_R * 0.55 + side * BUN_R * 0.35 * math.copysign(1, math.sin(th) + 1e-9)

    n = 64
    for i in range(n):
        th = -math.pi + 2 * math.pi * (i + RNG.uniform(0.2, 0.8)) / n
        z0 = hairline_z(th) + 0.003
        dz = (z0 - HC.z) / 0.12
        rxy = math.sqrt(max(0.0, 1 - min(0.95, dz) ** 2))
        d0 = Vector((math.sin(th) * rxy, -math.cos(th) * rxy, dz)).normalized()
        target = (BUN + Vector((RNG.uniform(-0.03, 0.03), RNG.uniform(-0.03, 0.0), -0.035)) - HC).normalized()
        pts, nrm = [], []
        steps = 18
        for k in range(steps + 1):
            t = k / steps
            dd = d0.slerp(target, t) if d0.dot(target) > -0.98 else d0
            p, nn = surf.hit(dd)
            lift = 0.0025 + 0.0035 * math.sin(math.pi * t)
            pts.append(p + nn * lift)
        pts = BC.catmull(pts[::2], 5)
        w0 = RNG.uniform(0.020, 0.030)
        up = (lambda i_, p_: (p_ - HC).normalized())
        tgt = base
        ribbon(tgt, pts, taper(w0, root=0.55, tip=0.25), lambda t: 0.0032 * (0.6 + 0.4 * math.sin(math.pi * t)), up)

    # bun: a core wound with thick clumps
    bm = bmesh.new()
    bmesh.ops.create_uvsphere(bm, u_segments=40, v_segments=28, radius=1.0)
    for v in bm.verts:
        v.co = BUN + Vector((v.co.x * BUN_R * 0.92, v.co.y * BUN_R * 0.90, v.co.z * BUN_R * 0.80))
    me = bpy.data.meshes.new("BunCore")
    bm.to_mesh(me)
    bm.free()
    core = BC.link(bpy.data.objects.new("BunCore", me))
    BC.shade_smooth(core)
    core.data.materials.append(mats["hair"])
    cuv = core.data.uv_layers.new(name="UVMap")
    for li in range(len(core.data.loops)):
        cuv.data[li].uv = (0.0, 0.6)
    for i in range(15):
        # loops wound around a mostly vertical axis, each tilted a bit differently
        axis = (bun_up + Vector((RNG.uniform(-0.9, 0.9), RNG.uniform(-0.9, 0.9), RNG.uniform(-0.3, 0.3)))).normalized()
        u = axis.orthogonal().normalized()
        u = Matrix.Rotation(RNG.uniform(0, 6.28), 3, axis) @ u
        w = axis.cross(u).normalized()
        h = RNG.uniform(-0.45, 0.55) * BUN_R
        rad = math.sqrt(max(0.0, BUN_R ** 2 - h * h)) * 0.98 + 0.006
        a0 = RNG.uniform(0, 6.28)
        span = RNG.uniform(1.3, 1.9) * math.pi
        pts = []
        for k in range(33):
            a = a0 + span * k / 32
            pts.append(BUN + axis * (h + 0.004 * math.sin(a * 2)) + (u * math.cos(a) + w * math.sin(a)) * rad)
        w0 = RNG.uniform(0.028, 0.038)
        up = (lambda i_, p_: (p_ - BUN).normalized())
        ribbon(base, pts, taper(w0, root=0.3, tip=0.1),
               lambda t, w0=w0: w0 * 0.30 * (0.35 + 0.65 * math.sin(math.pi * t) ** 0.5), up, ring=10)

    # face-framing locks in front of the ears, gentle S-curves
    for sx in (-1, 1):
        for (x0, y0, z0, drop, out, fwd, w0, waves) in (
                (0.066, -0.074, 1.598, 0.190, 0.028, -0.012, 0.0115, 2.0),
                (0.080, -0.052, 1.585, 0.160, 0.034, -0.004, 0.0095, 2.5),
                (0.074, -0.030, 1.570, 0.120, 0.030, 0.008, 0.0085, 3.0)):
            pts = []
            for k in range(9):
                t = k / 8
                x = XC + sx * (x0 + out * t + 0.006 * math.sin(math.pi * waves * t))
                y = y0 + fwd * t + 0.004 * math.cos(math.pi * waves * t)
                pts.append(V(x, y, z0 - drop * t ** 1.1))
            pts = BC.catmull(pts, 5)
            up = (lambda i_, p_, sx=sx: Vector((sx * 0.8, -0.6, 0.0)).normalized())
            ribbon(base, pts, taper(w0, root=0.8, tip=0.08, power=0.9),
                   lambda t, w0=w0: w0 * 0.42 * (1 - 0.75 * t), up)
    # nape wisps
    for i, x in enumerate((-0.06, -0.035, -0.01, 0.02, 0.045, 0.07)):
        z0 = 1.470 + RNG.uniform(-0.005, 0.01)
        p0, n0 = surf.hit((V(XC + x, 0.095, z0) - HC).normalized())
        pts = [p0 + n0 * 0.002]
        for k in range(1, 6):
            t = k / 5
            pts.append(p0 + n0 * (0.004 + 0.006 * t) + V(0.006 * math.sin(i + 3 * t), 0.004 * t, -0.045 * t - 0.01 * (i % 2) * t))
        pts = BC.catmull(pts, 4)
        ribbon(base, pts, taper(0.009, root=0.9, tip=0.1), lambda t: 0.0028 * (1 - 0.6 * t),
               lambda i_, p_: (p_ - HC).normalized(), ring=6)
    # a few flyaways off the bun and the side volume
    for i in range(9):
        dd = Vector((RNG.uniform(-1, 1), RNG.uniform(-0.2, 1), RNG.uniform(-0.1, 1))).normalized()
        start = BUN + dd * BUN_R * 0.9
        side = dd.orthogonal().normalized()
        pts = [start + dd * 0.012 * k + side * 0.010 * math.sin(k * 1.4) - V(0, 0, 0.003 * k * k) for k in range(6)]
        ribbon(base, BC.catmull(pts, 4), lambda t: 0.004 * (1 - t) + 0.0008, lambda t: 0.0016 * (1 - t) + 0.0004,
               None, ring=5)

    objs = [core]
    for name, batch, m in (("HairClumps", base, mats["hair"]), ("HairClumpsLight", light, mats["hair_light"]),
                           ("HairClumpsDark", dark, mats["hair_dark"])):
        if batch.v:
            ob = batch.build(name, m)
            objs.append(ob)
    return objs


# ---------------------------------------------------------------------------
# Materials: PBR for export, toon for renders
# ---------------------------------------------------------------------------
def pbr(name, hexcol, image=None, rough=0.8):
    m = BC.material(name, hexcol, rough=rough, image=image, spec=0.15)
    m["toon_base"] = hexcol
    return m


TOON_SHADE = {"Skin": "skin_shade", "Face": "skin_shade", "Hair": "hair_shade_rel", "HairLight": "hair",
              "HairDark": "hair_shade", "Sweater": "sweater_shade"}


def toonify(m, shade_hex=None, shade_mul=0.62, threshold=0.42, soft=0.03):
    """Rebuild a material as a 2-tone cel shader: Diffuse -> Shader to RGB -> constant ramp."""
    nt = m.node_tree
    tex_img = None
    for n in nt.nodes:
        if n.type == "TEX_IMAGE":
            tex_img = n.image
    base = BC.srgb(m.get("toon_base", "#CCCCCC"))
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    diff = nt.nodes.new("ShaderNodeBsdfDiffuse")
    diff.inputs["Color"].default_value = (1, 1, 1, 1)
    s2r = nt.nodes.new("ShaderNodeShaderToRGB")
    nt.links.new(diff.outputs["BSDF"], s2r.inputs["Shader"])
    bw = nt.nodes.new("ShaderNodeRGBToBW")
    nt.links.new(s2r.outputs["Color"], bw.inputs["Color"])
    ramp = nt.nodes.new("ShaderNodeValToRGB")
    ramp.color_ramp.interpolation = "EASE"
    ramp.color_ramp.elements[0].position = threshold - soft
    ramp.color_ramp.elements[0].color = (0, 0, 0, 1)
    ramp.color_ramp.elements[1].position = threshold + soft
    ramp.color_ramp.elements[1].color = (1, 1, 1, 1)
    nt.links.new(bw.outputs["Val"], ramp.inputs["Fac"])
    lit = nt.nodes.new("ShaderNodeRGB")
    lit.outputs[0].default_value = base
    col = lit.outputs[0]
    if tex_img is not None:
        t = nt.nodes.new("ShaderNodeTexImage")
        t.image = tex_img
        col = t.outputs["Color"]
    shade = nt.nodes.new("ShaderNodeMix")
    shade.data_type = "RGBA"
    shade.blend_type = "MULTIPLY"
    shade.inputs["Factor"].default_value = 1.0
    nt.links.new(col, shade.inputs["A"])
    sc = BC.srgb(shade_hex) if shade_hex else (shade_mul, shade_mul * 0.93, shade_mul * 1.02, 1)
    if shade_hex:   # express the shadow colour relative to the lit colour
        sc = tuple(min(1.0, sc[i] / max(base[i], 1e-3)) for i in range(3)) + (1,)
    shade.inputs["B"].default_value = sc
    mix = nt.nodes.new("ShaderNodeMix")
    mix.data_type = "RGBA"
    nt.links.new(ramp.outputs["Color"], mix.inputs["Factor"])
    nt.links.new(shade.outputs["Result"], mix.inputs["A"])
    nt.links.new(col, mix.inputs["B"])
    # thin rim light, the usual anime accent
    lw = nt.nodes.new("ShaderNodeLayerWeight")
    lw.inputs["Blend"].default_value = 0.12
    rim = nt.nodes.new("ShaderNodeMath")
    rim.operation = "GREATER_THAN"
    rim.inputs[1].default_value = 0.72
    nt.links.new(lw.outputs["Facing"], rim.inputs[0])
    rimk = nt.nodes.new("ShaderNodeMath")
    rimk.operation = "MULTIPLY"
    rimk.inputs[1].default_value = 0.10
    nt.links.new(rim.outputs["Value"], rimk.inputs[0])
    addr = nt.nodes.new("ShaderNodeMix")
    addr.data_type = "RGBA"
    addr.blend_type = "SCREEN"
    nt.links.new(rimk.outputs["Value"], addr.inputs["Factor"])
    nt.links.new(mix.outputs["Result"], addr.inputs["A"])
    addr.inputs["B"].default_value = (1, 0.93, 0.88, 1)
    em = nt.nodes.new("ShaderNodeEmission")
    nt.links.new(addr.outputs["Result"], em.inputs["Color"])
    nt.links.new(em.outputs["Emission"], out.inputs["Surface"])


def add_outlines(objs, ink_hex=PAL["ink"]):
    ink = bpy.data.materials.new("Ink")
    ink.use_nodes = True
    nt = ink.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    em = nt.nodes.new("ShaderNodeEmission")
    em.inputs["Color"].default_value = BC.srgb(ink_hex)
    nt.links.new(em.outputs["Emission"], out.inputs["Surface"])
    ink.use_backface_culling = True
    for ob, thick in objs:
        ob.data.materials.append(ink)
        so = ob.modifiers.new("InkOutline", "SOLIDIFY")
        so.thickness = thick
        so.offset = 1.0
        so.use_flip_normals = True
        so.use_rim = False
        so.material_offset = len(ob.data.materials) - 1


def join_objects(objs, name):
    for o in bpy.context.view_layer.objects:
        o.select_set(False)
    for o in objs:
        o.select_set(True)
    bpy.context.view_layer.objects.active = objs[0]
    bpy.ops.object.join()
    ob = objs[0]
    ob.name = ob.data.name = name
    BC.shade_smooth(ob)
    return ob


def relax(ob, iterations=6, lam=0.8):
    """Laplacian smoothing with volume preservation: removes the fitter's small ripples."""
    m = ob.modifiers.new("Relax", "LAPLACIANSMOOTH")
    m.iterations = iterations
    m.lambda_factor = lam
    m.use_volume_preserve = True
    m.use_normalized = True
    # keep modifiers already baked; this one is applied now
    order = [x for x in ob.modifiers if x.name != "Relax"]
    for x in order:
        x.show_render = False
        x.show_viewport = False
    dg = bpy.context.evaluated_depsgraph_get()
    me = bpy.data.meshes.new_from_object(ob.evaluated_get(dg))
    ob.modifiers.remove(m)
    for x in order:
        x.show_render = True
        x.show_viewport = True
    old = ob.data
    # vertex groups are stored per object; copying the mesh keeps weights
    ob.data = me
    me.name = old.name
    bpy.data.meshes.remove(old)


# ---------------------------------------------------------------------------
# Animation: smooth, seamlessly looping clips
# ---------------------------------------------------------------------------
def smooth01(x):
    """0..1 periodic bump with continuous derivatives (no kinks anywhere)."""
    return 0.5 - 0.5 * math.cos(x)


def build_clips(rig, fps=30):
    """Idle (3 s) and Walk (1.1 s). Every frame is keyed with LINEAR interpolation and the last
    key equals the first, so playback has constant speed through the loop point (Bezier keys
    ease to a stop at each end, which is what makes a loop stutter)."""
    prefs = bpy.context.preferences.edit
    prefs.keyframe_new_interpolation_type = "LINEAR"
    sc = bpy.context.scene
    sc.render.fps = fps
    rig.animation_data_create()

    def clip(name, frames, pose_fn):
        act = bpy.data.actions.new(name)
        act.use_fake_user = True
        rig.animation_data.action = act
        for f in range(frames + 1):
            ph = 2 * math.pi * f / frames
            rots, loc = pose_fn(ph)
            for pb in rig.pose.bones:
                pb.rotation_mode = "XYZ"
                pb.rotation_euler = rots.get(pb.name, (0, 0, 0))
                pb.keyframe_insert("rotation_euler", frame=f + 1)
            hp = rig.pose.bones["hips"]
            hp.location = loc
            hp.keyframe_insert("location", frame=f + 1)
        return act

    def idle(ph):
        s1, s2 = math.sin(ph), math.sin(ph * 2 + 0.7)
        r = {
            "spine": (0.010 * s1, 0.0, 0.006 * math.sin(ph + 1.0)),
            "chest": (0.022 * s1, 0.0, 0.0),                       # breathing
            "neck": (-0.012 * s1, 0.020 * math.sin(ph + 0.5), 0.0),
            "head": (0.010 * s2, -0.030 * math.sin(ph + 0.9), 0.012 * math.sin(ph + 2.0)),
        }
        for side, sg in (("L", 1), ("R", -1)):
            r[f"upper_arm.{side}"] = (0.025 * math.sin(ph + 0.4), 0.0, 0.0)
            r[f"forearm.{side}"] = (0.08 + 0.02 * math.sin(ph + 0.8), 0.0, 0.0)
            r[f"hand.{side}"] = (0.04 * math.sin(ph + 1.2), 0.0, 0.0)
        # weight shift: hips sway a little side to side, legs compensate
        sway = 0.006 * math.sin(ph)
        return r, (sway, 0.003 * math.sin(2 * ph), 0.0)

    def walk(ph):
        r = {}
        for side, sg, off in (("L", 1, 0.0), ("R", -1, math.pi)):
            p = ph + off
            sw = math.sin(p)                                     # +1 leg forward
            r[f"thigh.{side}"] = (-0.44 * sw, 0.0, 0.0)
            # knee bends while the leg swings forward (max as the legs pass), straight at heel strike
            swing = ((1 + math.cos(p - 0.5)) / 2) ** 3
            r[f"shin.{side}"] = (0.06 + 0.70 * swing, 0.0, 0.0)
            push = ((1 - math.sin(p)) / 2) ** 4                 # push-off when the leg is behind
            r[f"foot.{side}"] = (0.10 * sw - 0.30 * swing + 0.20 * push, 0.0, 0.0)
            r[f"toe.{side}"] = (-0.35 * push, 0.0, 0.0)
            r[f"upper_arm.{side}"] = (0.30 * sw, 0.0, 0.0)       # arms swing against the legs
            r[f"forearm.{side}"] = (0.22 + 0.12 * smooth01(p), 0.0, 0.0)
        r["spine"] = (0.04, 0.05 * math.sin(ph), 0.0)
        r["chest"] = (0.0, -0.09 * math.sin(ph), 0.015 * math.sin(2 * ph))
        r["neck"] = (0.0, 0.04 * math.sin(ph), 0.0)
        r["head"] = (-0.02, 0.03 * math.sin(ph), 0.0)
        # two bobs per cycle, lowest when the feet pass each other
        return r, (0.0, -0.012 + 0.012 * math.cos(2 * ph), 0.0)

    acts = [clip("Idle", 90, idle), clip("Walk", 33, walk)]
    for act in acts:
        for fc in iter_fcurves(act):
            for kp in fc.keyframe_points:
                kp.interpolation = "LINEAR"
    for act in acts:
        tr = rig.animation_data.nla_tracks.new()
        tr.name = act.name
        tr.strips.new(act.name, 1, act)
        tr.mute = True
    rig.animation_data.action = None
    for pb in rig.pose.bones:
        pb.rotation_euler = (0, 0, 0)
        pb.location = (0, 0, 0)


def fix_quaternion_signs(glb_path):
    """Make every rotation track sign-continuous (q and -q are the same rotation, but engines
    that blend quaternions linearly twitch where the sign flips between keys)."""
    import json
    import struct
    data = bytearray(open(glb_path, "rb").read())
    jlen = struct.unpack_from("<I", data, 12)[0]
    doc = json.loads(bytes(data[20:20 + jlen]))
    bin0 = 20 + jlen + 8
    fixed = 0
    for an in doc.get("animations", []):
        for ch in an["channels"]:
            if ch["target"]["path"] != "rotation":
                continue
            a = doc["accessors"][an["samplers"][ch["sampler"]]["output"]]
            bv = doc["bufferViews"][a["bufferView"]]
            off = bin0 + bv.get("byteOffset", 0) + a.get("byteOffset", 0)
            q = np.frombuffer(bytes(data[off:off + a["count"] * 16]), dtype=np.float32).reshape(-1, 4).copy()
            for k in range(1, len(q)):
                if np.dot(q[k], q[k - 1]) < 0:
                    q[k] = -q[k]
                    fixed += 1
            data[off:off + a["count"] * 16] = q.astype(np.float32).tobytes()
    open(glb_path, "wb").write(bytes(data))
    return fixed


def iter_fcurves(act):
    if hasattr(act, "fcurves") and len(getattr(act, "fcurves", [])):
        yield from act.fcurves
        return
    for layer in getattr(act, "layers", []):
        for strip in layer.strips:
            for bag in getattr(strip, "channelbags", []):
                yield from bag.fcurves


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------
def setup_eevee(w=520, h=1080):
    sc = bpy.context.scene
    sc.render.engine = "BLENDER_EEVEE"
    sc.render.film_transparent = True
    sc.render.resolution_x, sc.render.resolution_y = w, h
    sc.view_settings.view_transform = "Standard"
    try:
        sc.eevee.taa_render_samples = 32
    except Exception:
        pass
    world = bpy.data.worlds.new("World")
    sc.world = world
    world.use_nodes = True
    world.node_tree.nodes["Background"].inputs[0].default_value = (1, 1, 1, 1)
    world.node_tree.nodes["Background"].inputs[1].default_value = 0.35
    L = bpy.data.lights.new("Key", "SUN")
    L.energy = 3.0
    L.angle = 0.05
    lo = BC.link(bpy.data.objects.new("Key", L))
    lo.rotation_euler = (math.radians(50), 0, math.radians(-35))
    cam = bpy.data.cameras.new("Cam")
    cam.type = "ORTHO"
    cam.ortho_scale = 1.9
    co = BC.link(bpy.data.objects.new("Cam", cam))
    sc.camera = co
    return co


def shoot(cam, az, path, z=0.90, scale=1.9, res=(520, 1080)):
    sc = bpy.context.scene
    sc.render.resolution_x, sc.render.resolution_y = res
    cam.data.ortho_scale = scale
    a = math.radians(az)
    cam.location = V(math.sin(a) * 4, -math.cos(a) * 4, z)
    cam.rotation_euler = (math.pi / 2, 0, a)
    # the key light follows the camera a little so every view is lit from the front-left
    key = bpy.data.objects["Key"]
    key.rotation_euler = (math.radians(55), 0, a - math.radians(35))
    sc.render.filepath = path
    bpy.ops.render.render(write_still=True)
    return path


def sheet(paths, out, ref=None):
    from PIL import Image
    ims = [Image.open(p).convert("RGBA") for p in paths]
    W = sum(i.width for i in ims)
    H = ims[0].height
    s = Image.new("RGBA", (W, H), (255, 255, 255, 255))
    x = 0
    for i in ims:
        s.alpha_composite(i, (x, 0))
        x += i.width
    if ref:
        r = Image.open(ref).convert("RGBA")
        bg = Image.new("RGBA", r.size, (255, 255, 255, 255))
        bg.alpha_composite(r)
        r = bg.resize((W, int(r.height * W / r.width)))
        both = Image.new("RGBA", (W, H + r.height), (255, 255, 255, 255))
        both.alpha_composite(r, (0, 0))
        both.alpha_composite(s, (0, r.height))
        both.convert("RGB").save(out.replace(".png", "_vs_reference.png"))
    s.convert("RGB").save(out)


# ---------------------------------------------------------------------------
def main():
    import json
    render = "--no-render" not in sys.argv
    BC.reset()
    face_img = face_texture()
    strand_img = strand_texture()
    plaid = BC.plaid_texture()
    mats = {
        "skin": pbr("Skin", PAL["skin"]), "face": pbr("Face", PAL["skin"], image=face_img),
        "hair": pbr("Hair", "#FFFFFF", image=strand_img, rough=0.5), "hair_light": pbr("HairLight", PAL["hair_light"], rough=0.5),
        "hair_dark": pbr("HairDark", PAL["hair_dark"], rough=0.5),
        "sweater": pbr("Sweater", PAL["sweater"], rough=0.95), "tee": pbr("Tee", PAL["tee"], rough=0.9),
        "plaid": pbr("Plaid", PAL["plaid"], image=plaid, rough=0.95), "string": pbr("Drawstring", PAL["string"]),
        "under": pbr("Underwear", PAL["under"]),
    }

    rig = BC.build_armature()
    # head + ears
    verts, faces = HD.build_mesh()
    head = BC.mesh_obj("Head", verts, faces, mats["face"])
    bm = bmesh.new()
    bm.from_mesh(head.data)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    bm.to_mesh(head.data)
    bm.free()
    head["from_drawing"] = True
    face_uvs(head)
    smooth_face_normals(head)
    ears = BC.build_ears_from_drawing(mats["skin"])

    # hair (its own slot)
    volume = build_hair_volume(head, mats["hair"])
    clumps = build_hair_clumps(volume, head, mats)
    hair = join_objects([volume] + clumps, "Hair")
    bm = bmesh.new()                    # swept clumps must face outward (outlines, backface culling)
    bm.from_mesh(hair.data)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    bm.to_mesh(hair.data)
    bm.free()
    BC.shade_smooth(hair)
    for o in [head] + ears + [hair]:
        BC.skin_to(o, rig, None, rigid="head")
    hair["wardrobe_slot"] = "hair"
    hair["label"] = "Messy bun"
    head["wardrobe_slot"] = "head"

    # body + clothes (separate meshes on one skeleton)
    body = BC.build_body(mats["skin"], mats["under"])
    BC.rig_body(body, rig)
    face_sec, face_bone, ws = BC.classify_body(body)
    under = BC.build_underwear(body, face_sec, ws, rig, mats["under"])
    items = {
        "Outfit_Sweater": BC.build_top("Outfit_Sweater", mats["sweater"], rig, body, face_sec, ws),
        "Outfit_TShirt": BC.build_top("Outfit_TShirt", mats["tee"], rig, body, face_sec, ws, ease=0.93, hem=0.925,
                                      sleeve_cut=1.150, sleeve_r=(0.053, 0.047, 0.047, 0.046), neck_depth=0.035),
        "Outfit_Joggers": BC.build_bottom("Outfit_Joggers", mats["plaid"], mats["string"], rig, body, face_sec, ws),
        "Outfit_Shorts": BC.build_bottom("Outfit_Shorts", mats["plaid"], mats["string"], rig, body, face_sec, ws,
                                         cut=0.640, leg_ease=1.04),
    }
    for name, slot, label, hides in BC.WARDROBE:
        ob = items[name]
        ob["wardrobe_slot"] = slot
        ob["label"] = label
        ob["hides"] = ",".join(hides)
    if "--fit" in sys.argv:
        BC.fit_to_reference(body, face_sec, items, head, ears, [])
        for ob in [items[n] for n in BC.DEFAULT_OUTFIT] + [body]:
            relax(ob)
    sections = BC.split_body(body, face_sec)
    sections.update(under)
    bpy.data.objects.remove(body)
    build_clips(rig)
    BC.show_outfit(items, sections, BC.DEFAULT_OUTFIT)

    # export (plain PBR materials)
    base = [rig, head, hair] + ears + list(sections.values())
    glb = os.path.join(OUT_EXPORT, "character.glb")
    BC.export_selection(glb, base + list(items.values()), True)
    print("quaternion sign fixes:", fix_quaternion_signs(glb))
    BC.glb_to_embedded_gltf(glb, os.path.join(OUT_EXPORT, "character.gltf"))
    parts = os.path.join(OUT_EXPORT, "parts")
    os.makedirs(parts, exist_ok=True)
    BC.export_selection(os.path.join(parts, "base_body.glb"), [rig, head] + ears + list(sections.values()), True)
    fix_quaternion_signs(os.path.join(parts, "base_body.glb"))
    BC.export_selection(os.path.join(parts, "hair_bun.glb"), [rig, hair], False)
    for name, ob in items.items():
        BC.export_selection(os.path.join(parts, name.replace("Outfit_", "").lower() + ".glb"), [rig, ob], False)
    manifest = {
        "skeleton": [b[0] for b in BC.BONES],
        "body_sections": sorted(sections),
        "default_outfit": BC.DEFAULT_OUTFIT + ["Hair"],
        "items": [{"node": "Hair", "slot": "hair", "label": "Messy bun", "hides": [], "file": "parts/hair_bun.glb"}] +
                 [{"node": n, "slot": sl, "label": lb, "hides": h,
                   "file": "parts/" + n.replace("Outfit_", "").lower() + ".glb"} for n, sl, lb, h in BC.WARDROBE],
    }
    with open(os.path.join(OUT_EXPORT, "wardrobe.json"), "w") as f:
        json.dump(manifest, f, indent=2)
    tris = sum(len(p.vertices) - 2 for o in bpy.data.objects if o.type == "MESH" and not o.hide_render
               for p in o.data.polygons)
    print(f"EXPORTED {glb} visible triangles={tris}")
    bpy.context.preferences.filepaths.save_version = 0
    bpy.ops.wm.save_as_mainfile(filepath=os.path.join(OUT_EXPORT, "character.blend"))

    if not render:
        return
    # renders: toon materials + outlines
    for m in list(bpy.data.materials):
        if m.get("toon_base"):
            key = TOON_SHADE.get(m.name)
            thr = {"Face": 0.16, "Skin": 0.30}.get(m.name, 0.42)
            toonify(m, shade_hex=PAL[key] if key else None, threshold=thr)
    outl = [(head, 0.0016), (hair, 0.0022)] + [(e, 0.0012) for e in ears] + \
           [(o, 0.0024) for o in items.values()] + [(o, 0.0020) for o in sections.values()]
    add_outlines(outl)
    rig.data.pose_position = "REST"
    cam = setup_eevee()
    views = [("front", 0), ("threequarter", -32), ("side", -90), ("back", 180), ("back_threequarter", -142)]
    paths = [shoot(cam, az, os.path.join(OUT_RENDER, f"anime_{n}.png")) for n, az in views]
    sheet(paths, os.path.join(OUT_RENDER, "anime_turnaround.png"), ref=os.path.join(ROOT, "reference", "turnaround.png"))
    extra = [shoot(cam, az, os.path.join(OUT_RENDER, f"anime_extra_{az}.png")) for az in (25, 60, 90, 130)]
    sheet(extra, os.path.join(OUT_RENDER, "anime_inbetween.png"))
    heads = [shoot(cam, az, os.path.join(OUT_RENDER, f"anime_head_{az}.png"), z=1.56, scale=0.46, res=(600, 600))
             for az in (0, -35, -90, 35, 150)]
    sheet(heads, os.path.join(OUT_RENDER, "anime_heads.png"))
    # wardrobe
    wp = []
    for cname, worn in (("base", []), ("sweater_joggers", ["Outfit_Sweater", "Outfit_Joggers"]),
                        ("tee_shorts", ["Outfit_TShirt", "Outfit_Shorts"]),
                        ("tee_joggers", ["Outfit_TShirt", "Outfit_Joggers"])):
        BC.show_outfit(items, sections, worn)
        wp.append(shoot(cam, -30, os.path.join(OUT_RENDER, f"anime_wardrobe_{cname}.png")))
    hair.hide_render = True
    wp.append(shoot(cam, -30, os.path.join(OUT_RENDER, "anime_wardrobe_nohair.png")))
    hair.hide_render = False
    sheet(wp, os.path.join(OUT_RENDER, "anime_wardrobe.png"))
    BC.show_outfit(items, sections, BC.DEFAULT_OUTFIT)
    # motion check: the walk cycle at six phases, side view
    rig.data.pose_position = "POSE"
    mp = []
    for f in (1, 6, 12, 17, 23, 28):
        BC.set_action(rig, "Walk", f)
        mp.append(shoot(cam, -90, os.path.join(OUT_RENDER, f"anime_walk_{f:02d}.png"), res=(360, 1000)))
    sheet(mp, os.path.join(OUT_RENDER, "anime_walk.png"))
    mp = []
    for f in (1, 12, 17, 28):
        BC.set_action(rig, "Walk", f)
        mp.append(shoot(cam, -30, os.path.join(OUT_RENDER, f"anime_walkq_{f:02d}.png"), res=(420, 1000)))
    sheet(mp, os.path.join(OUT_RENDER, "anime_walk_threequarter.png"))
    BC.set_action(rig, None, 1)
    rig.data.pose_position = "REST"
    print("RENDERED")


if __name__ == "__main__":
    main()
