"""Procedural build of the "cozy pajamas" character from reference/turnaround.png.

Run with the Blender Python module (pip install bpy) or inside Blender:

    python3 character/blender/build_character.py            # build + export + renders
    python3 character/blender/build_character.py --no-render

Everything is generated from the parameters below so the model can be iterated
by tweaking numbers and re-running. Units are meters, Z up, character faces -Y
(which becomes +Z forward in glTF).
"""
import math
import os
import random
import sys

import bpy
import bmesh
from mathutils import Vector, Matrix
from mathutils.bvhtree import BVHTree

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT_EXPORT = os.path.join(ROOT, "export")
OUT_RENDER = os.path.join(ROOT, "renders")
OUT_TEX = os.path.join(ROOT, "textures")
for d in (OUT_EXPORT, OUT_RENDER, OUT_TEX):
    os.makedirs(d, exist_ok=True)

RNG = random.Random(7)

# --------------------------------------------------------------------------
# Palette (sRGB hex, sampled from the reference sheet)
# --------------------------------------------------------------------------
COL = {
    "skin": "#E3A47C",
    "skin_shadow": "#C98362",
    "shirt": "#2F3A6E",
    "pants": "#6F7BAC",
    "pants_line": "#CFCFDF",
    "pants_line2": "#9AA6D6",
    "hair": "#6A3A1F",
    "hair_dark": "#57301A",
    "string": "#ECE4D6",
    "brow": "#4A2A17",
    "iris": "#6B3A1C",
    "lip": "#B8615A",
    "blush": "#EE8C7C",
    "nail": "#EFB79A",
}


def srgb(hexstr, a=1.0):
    h = hexstr.lstrip("#")
    c = [int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4)]
    lin = [x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
    return (*lin, a)


def rgb255(hexstr):
    h = hexstr.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


# --------------------------------------------------------------------------
# Scene helpers
# --------------------------------------------------------------------------
def reset():
    bpy.ops.wm.read_factory_settings(use_empty=True)


def link(obj):
    bpy.context.scene.collection.objects.link(obj)
    return obj


def mesh_obj(name, verts, faces, mat=None, smooth=True):
    me = bpy.data.meshes.new(name)
    me.from_pydata([tuple(v) for v in verts], [], faces)
    me.update()
    ob = link(bpy.data.objects.new(name, me))
    if smooth:
        for p in me.polygons:
            p.use_smooth = True
    if mat:
        me.materials.append(mat)
    return ob


def bake(ob):
    """Apply all modifiers of `ob` in place (without bpy.ops)."""
    dg = bpy.context.evaluated_depsgraph_get()
    me = bpy.data.meshes.new_from_object(ob.evaluated_get(dg))
    old = ob.data
    ob.modifiers.clear()
    ob.data = me
    me.name = ob.name
    bpy.data.meshes.remove(old)
    return ob


def shade_smooth(ob):
    for p in ob.data.polygons:
        p.use_smooth = True


def material(name, color, rough=0.75, image=None, spec=0.25):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    nt = m.node_tree
    bsdf = nt.nodes.get("Principled BSDF")
    bsdf.inputs["Base Color"].default_value = srgb(color) if isinstance(color, str) else color
    bsdf.inputs["Roughness"].default_value = rough
    if "Specular IOR Level" in bsdf.inputs:
        bsdf.inputs["Specular IOR Level"].default_value = spec
    if image is not None:
        tex = nt.nodes.new("ShaderNodeTexImage")
        tex.image = image
        tex.location = (-400, 200)
        nt.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
    m.diffuse_color = srgb(color) if isinstance(color, str) else color
    return m


# --------------------------------------------------------------------------
# Skin-modifier "tube skeleton" meshes (clean quad topology)
# --------------------------------------------------------------------------
def skin_mesh(name, nodes, edges, root=0, subdiv=2, mat=None):
    """nodes: list of (co, (rx, ry)). Builds a skin-modifier mesh and bakes it."""
    me = bpy.data.meshes.new(name)
    me.from_pydata([n[0] for n in nodes], edges, [])
    me.update()
    ob = link(bpy.data.objects.new(name, me))
    sk = ob.modifiers.new("Skin", "SKIN")
    sk.use_smooth_shade = True
    sk.branch_smoothing = 0.6
    layer = me.skin_vertices[0].data
    for i, n in enumerate(nodes):
        layer[i].radius = n[1]
        layer[i].use_root = (i == root)
    sd = ob.modifiers.new("Subsurf", "SUBSURF")
    sd.levels = subdiv
    sd.render_levels = subdiv
    bake(ob)
    shade_smooth(ob)
    if mat:
        ob.data.materials.append(mat)
    return ob


def V(x, y, z):
    return Vector((x, y, z))


def mirror_x(v):
    return Vector((-v.x, v.y, v.z))


# --------------------------------------------------------------------------
# Sweep: tube along a path with elliptical cross-section (hair, strings)
# --------------------------------------------------------------------------
def catmull(ctrl, n=8, closed=False):
    pts = [Vector(c) for c in ctrl]
    if closed:
        P = [pts[-1]] + pts + [pts[0], pts[1]]
        segs = len(pts)
    else:
        P = [pts[0] * 2 - pts[1]] + pts + [pts[-1] * 2 - pts[-2]]
        segs = len(pts) - 1
    out = []
    for i in range(segs):
        p0, p1, p2, p3 = P[i], P[i + 1], P[i + 2], P[i + 3]
        for k in range(n):
            t = k / n
            t2, t3 = t * t, t * t * t
            out.append(0.5 * ((2 * p1) + (-p0 + p2) * t + (2 * p0 - 5 * p1 + 4 * p2 - p3) * t2
                              + (-p0 + 3 * p1 - 3 * p2 + p3) * t3))
    if not closed:
        out.append(pts[-1].copy())
    return out


def sweep_geo(pts, width, thick, up=None, ring=8, closed=False, cap=True):
    """Return (verts, faces) of a tube. width/thick: callables t->half-size or floats.
    up: callable(i, p) -> vector the 'thick' axis should align with (e.g. surface normal)."""
    n = len(pts)
    wf = width if callable(width) else (lambda t, w=width: w)
    hf = thick if callable(thick) else (lambda t, h=thick: h)
    verts, faces = [], []
    prev_up = None
    for i, p in enumerate(pts):
        if closed:
            T = (pts[(i + 1) % n] - pts[(i - 1) % n])
        else:
            T = pts[min(i + 1, n - 1)] - pts[max(i - 1, 0)]
        T.normalize()
        if up is not None:
            U = Vector(up(i, p))
        elif prev_up is not None:
            U = prev_up
        else:
            U = Vector((0, 0, 1)) if abs(T.z) < 0.9 else Vector((1, 0, 0))
        B = T.cross(U)
        if B.length < 1e-6:
            B = T.orthogonal()
        B.normalize()
        N = B.cross(T).normalized()
        prev_up = N
        t = i / (n - 1 if not closed else n)
        w, h = wf(t), hf(t)
        for k in range(ring):
            a = 2 * math.pi * k / ring
            verts.append(p + B * (w * math.cos(a)) + N * (h * math.sin(a)))
    segs = n if closed else n - 1
    for i in range(segs):
        i2 = (i + 1) % n
        for k in range(ring):
            k2 = (k + 1) % ring
            faces.append((i * ring + k, i * ring + k2, i2 * ring + k2, i2 * ring + k))
    if cap and not closed:
        for idx, pt in ((0, pts[0]), (n - 1, pts[-1])):
            verts.append(pt.copy())
            c = len(verts) - 1
            for k in range(ring):
                k2 = (k + 1) % ring
                a, b = idx * ring + k, idx * ring + k2
                faces.append((a, b, c) if idx == n - 1 else (b, a, c))
    return verts, faces


class GeoBatch:
    """Accumulates many swept pieces into one mesh object."""

    def __init__(self):
        self.v, self.f = [], []

    def add(self, verts, faces):
        o = len(self.v)
        self.v.extend(verts)
        self.f.extend(tuple(i + o for i in f) for f in faces)

    def build(self, name, mat):
        return mesh_obj(name, self.v, self.f, mat)


# --------------------------------------------------------------------------
# Textures (generated with numpy/PIL so everything stays reproducible)
# --------------------------------------------------------------------------
def save_image(path, pil_img, name):
    pil_img.save(path)
    img = bpy.data.images.load(path)
    img.name = name
    img.pack()
    return img


def plaid_texture(size=1024):
    import numpy as np
    from PIL import Image
    base = np.array(rgb255(COL["pants"]), dtype=float)
    line = np.array(rgb255(COL["pants_line"]), dtype=float)
    line2 = np.array(rgb255(COL["pants_line2"]), dtype=float)
    img = np.tile(base, (size, size, 1))
    yy, xx = np.mgrid[0:size, 0:size] / size
    rng = np.random.default_rng(3)
    # fabric noise
    img *= (0.96 + 0.05 * rng.random((size, size)))[..., None]
    # tile: 4 checks per texture; each check has a thin bright line + a faint secondary line
    rep = 4

    def stripe(c, pos, w):
        d = np.abs(((c * rep - pos) + 0.5) % 1.0 - 0.5)
        return np.clip(1.0 - d / w, 0, 1) ** 0.7

    s1 = np.maximum(stripe(xx, 0.0, 0.022), stripe(yy, 0.0, 0.022)) * 0.75
    s2 = np.maximum(stripe(xx, 0.5, 0.012), stripe(yy, 0.5, 0.012)) * 0.35
    img = img * (1 - s2[..., None]) + line2 * s2[..., None]
    img = img * (1 - s1[..., None] * 0.85) + line * (s1[..., None] * 0.85)
    im = Image.fromarray(np.clip(img, 0, 255).astype("uint8"), "RGB")
    return save_image(os.path.join(OUT_TEX, "pants_plaid.png"), im, "pants_plaid")


# Face texture uses a front planar projection: u = (x-FX0)/FW, v = (z-FZ0)/FH
FX0, FW = -0.105, 0.21
FZ0, FH = 1.40, 0.21


def face_texture(size=1024, ss=3):
    from PIL import Image, ImageDraw, ImageFilter
    S = size * ss
    skin = rgb255(COL["skin"])
    im = Image.new("RGBA", (S, S), skin + (255,))

    def P(x, z):
        return ((x - FX0) / FW * S, (1 - (z - FZ0) / FH) * S)

    def M(m):
        return m / FW * S

    # blush
    blush = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    bd = ImageDraw.Draw(blush)
    for sx in (-1, 1):
        cx, cz = sx * 0.060, 1.497
        a, b = P(cx - 0.020, cz + 0.011), P(cx + 0.020, cz - 0.011)
        bd.ellipse([a, b], fill=rgb255(COL["blush"]) + (110,))
    blush = blush.filter(ImageFilter.GaussianBlur(M(0.008)))
    im.alpha_composite(blush)

    d = ImageDraw.Draw(im)
    brow = rgb255(COL["brow"])
    lash = (44, 24, 16, 255)
    for sx in (-1, 1):
        cx, cz = sx * 0.043, 1.534
        inner, outer = cx - sx * 0.022, cx + sx * 0.023

        def lid(t, top):
            x = inner + (outer - inner) * t
            # almond: upper lid peaks a bit toward the outer side
            if top:
                z = cz + 0.001 + 0.0200 * math.sin(math.pi * t) ** 0.8 + 0.004 * t
            else:
                z = cz - 0.002 - 0.0135 * math.sin(math.pi * t) ** 0.9 + 0.003 * t
            return x, z

        up = [lid(i / 24, True) for i in range(25)]
        lo = [lid(i / 24, False) for i in range(25)]
        shape = [P(*q) for q in up] + [P(*q) for q in reversed(lo)]
        # eye white + iris clipped to the eye shape
        eye = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        ed = ImageDraw.Draw(eye)
        ed.polygon(shape, fill=(250, 245, 238, 255))
        ix, iz = cx + sx * 0.0005, cz + 0.0035
        r = 0.0148
        ed.ellipse([P(ix - r, iz + r), P(ix + r, iz - r)], fill=rgb255(COL["iris"]) + (255,))
        r2 = 0.0110
        ed.ellipse([P(ix - r2, iz + r2 - 0.002), P(ix + r2, iz - r2 - 0.002)], fill=(150, 92, 50, 255))
        r3 = 0.0060
        ed.ellipse([P(ix - r3, iz + r3), P(ix + r3, iz - r3)], fill=(28, 16, 10, 255))
        h = 0.0032
        hx, hz = ix + 0.004, iz + 0.005
        ed.ellipse([P(hx - h, hz + h), P(hx + h, hz - h)], fill=(255, 255, 255, 255))
        h2 = 0.0016
        hx, hz = ix - 0.005, iz - 0.005
        ed.ellipse([P(hx - h2, hz + h2), P(hx + h2, hz - h2)], fill=(255, 255, 255, 230))
        mask = Image.new("L", (S, S), 0)
        ImageDraw.Draw(mask).polygon(shape, fill=255)
        eye.putalpha(Image.composite(eye.getchannel("A"), Image.new("L", (S, S), 0), mask))
        # soft lid shadow on the top of the eyeball
        im.alpha_composite(eye)
        d = ImageDraw.Draw(im)
        # upper lash line (thick, with outer flick) and thin lower line
        pts = [P(*q) for q in up]
        for i in range(len(pts) - 1):
            t = i / (len(pts) - 1)
            w = M(0.0012 + 0.0032 * math.sin(math.pi * min(1, t * 1.15)) ** 0.5)
            d.line([pts[i], pts[i + 1]], fill=lash, width=max(1, int(w)))
            d.ellipse([pts[i][0] - w / 2, pts[i][1] - w / 2, pts[i][0] + w / 2, pts[i][1] + w / 2], fill=lash)
        ox, oz = up[-1]
        d.line([P(ox, oz), P(ox + sx * 0.006, oz + 0.004)], fill=lash, width=int(M(0.0022)))
        lpts = [P(*q) for q in lo[4:22]]
        d.line(lpts, fill=(150, 95, 70, 200), width=int(M(0.0009)))
        # crease above the eye
        cpts = [P(inner + (outer - inner) * t, cz + 0.024 + 0.004 * math.sin(math.pi * t)) for t in
                [i / 12 for i in range(2, 11)]]
        d.line(cpts, fill=rgb255(COL["skin_shadow"]) + (255,), width=int(M(0.0010)))
        # eyebrow: tapered arc
        bpts = []
        for i in range(21):
            t = i / 20
            x = cx - sx * 0.021 + sx * 0.047 * t
            z = 1.566 + 0.010 * math.sin(math.pi * (t * 0.85 + 0.1)) - 0.004 * t
            bpts.append((P(x, z), M(0.0055 * (1 - 0.75 * t) + 0.0012)))
        for (p, w) in bpts:
            d.ellipse([p[0] - w / 2, p[1] - w / 2, p[0] + w / 2, p[1] + w / 2], fill=brow + (255,))

    # nose: a faint shadow under the tip
    a_, b_ = P(-0.007, 1.4735), P(0.007, 1.4695)
    d.ellipse([a_, b_], fill=rgb255(COL["skin_shadow"]) + (70,))
    # mouth: small closed smile
    mp = [P(-0.017 + 0.034 * t, 1.4500 - 0.0045 * math.sin(math.pi * t) + 0.0022 * (abs(t - 0.5) * 2) ** 2)
          for t in [i / 20 for i in range(21)]]
    d.line(mp, fill=(140, 60, 52, 255), width=int(M(0.0018)), joint="curve")
    # lower lip hint
    lp = [P(-0.009 + 0.018 * t, 1.4435 - 0.0025 * math.sin(math.pi * t)) for t in [i / 10 for i in range(11)]]
    d.line(lp, fill=rgb255(COL["lip"]) + (160,), width=int(M(0.0022)), joint="curve")

    im = im.resize((size, size), Image.LANCZOS).convert("RGB")
    return save_image(os.path.join(OUT_TEX, "face.png"), im, "face")


# --------------------------------------------------------------------------
# Body parts
# --------------------------------------------------------------------------
HEAD_C = V(0.0, -0.014, 1.510)
HEAD_R = V(0.101, 0.103, 0.114)
# face texture features are authored for a 0.090 x 0.104 head; scale into the real head
KX, KZ = HEAD_R.x / 0.090, HEAD_R.z / 0.104


def face_uv(x, z):
    fx = x / KX
    fz = (z - HEAD_C.z) / KZ + 1.522
    return (fx - FX0) / FW, (fz - FZ0) / FH


def build_head(mat):
    bm = bmesh.new()
    bmesh.ops.create_uvsphere(bm, u_segments=48, v_segments=32, radius=1.0)
    for v in bm.verts:
        x, y, z = v.co
        t = max(0.0, -z)          # lower half
        up = max(0.0, z)
        # jaw / chin taper
        x *= 1.0 - 0.26 * t ** 2.2
        ry = 1.0
        if y > 0:                 # back of the head
            ry *= 1.0 - 0.38 * t ** 1.2 + 0.06 * up
        else:                     # face
            ry *= 1.0 - 0.10 * t ** 2
        y *= ry
        # slightly flatter face plane
        if y < 0:
            y *= 0.97 + 0.03 * min(1, abs(x) * 1.5)
        if y < 0 and z < 0:       # longer lower face so the chin clears the neck
            z *= 1.0 + 0.16 * t * min(1.0, -y * 2.0)
        co = Vector((x * HEAD_R.x, y * HEAD_R.y, z * HEAD_R.z))
        # chin forward
        if co.y < 0:
            co.y -= 0.016 * t ** 3
        # cheeks
        for sx in (-1, 1):
            dx, dz = (co.x - sx * 0.060) / 0.034, (co.z + 0.03) / 0.034
            if co.y < 0:
                co.y -= 0.004 * math.exp(-(dx * dx + dz * dz))
        # nose bump
        if co.y < 0:
            nz = co.z - (1.482 - 1.522) * KZ
            g = math.exp(-(co.x / 0.0095) ** 2 - (nz / (0.020 if nz > 0 else 0.008)) ** 2)
            co.y -= 0.017 * g * (0.5 + 0.5 * min(1, max(0, 1 - nz / 0.03)))
            # mouth / muzzle and a little chin
            mz = co.z - (1.450 - 1.522) * KZ
            co.y -= 0.006 * math.exp(-(co.x / 0.025) ** 2 - (mz / 0.012) ** 2)
            cz_ = co.z - (1.425 - 1.522) * KZ
            co.y -= 0.004 * math.exp(-(co.x / 0.018) ** 2 - (cz_ / 0.008) ** 2)
            # eye sockets slightly inset, brow slopes back above
            for sx in (-1, 1):
                ez = co.z - (1.535 - 1.522) * KZ
                co.y += 0.004 * math.exp(-((co.x - sx * 0.047) / 0.022) ** 2 - (ez / 0.014) ** 2)
            if co.z > 0.045:
                co.y += 0.06 * (co.z - 0.045) ** 1.5 * 3
        v.co = co + HEAD_C
    me = bpy.data.meshes.new("Head")
    bm.to_mesh(me)
    bm.free()
    ob = link(bpy.data.objects.new("Head", me))
    shade_smooth(ob)
    ob.data.materials.append(mat)
    # UVs: front planar projection for the face, back of head -> a skin corner
    uv = me.uv_layers.new(name="UVMap")
    for loop in me.loops:
        co = me.vertices[loop.vertex_index].co
        if co.y < HEAD_C.y + 0.02:
            u, vv = face_uv(co.x, co.z)
        else:
            u, vv = 0.01, 0.01
        uv.data[loop.index].uv = (min(max(u, 0.005), 0.995), min(max(vv, 0.005), 0.995))
    return ob


def build_ears(mat):
    objs = []
    for sx in (-1, 1):
        bm = bmesh.new()
        bmesh.ops.create_uvsphere(bm, u_segments=16, v_segments=10, radius=1.0)
        for v in bm.verts:
            x, y, z = v.co
            v.co = Vector((x * 0.009, y * 0.018, z * 0.027))
        me = bpy.data.meshes.new("Ear")
        bm.to_mesh(me)
        bm.free()
        ob = link(bpy.data.objects.new("Ear", me))
        mw = Matrix.Translation(V(sx * 0.096, 0.004, HEAD_C.z - 0.017)) @ Matrix.Rotation(-sx * 0.25, 4, "Z") @ Matrix.Rotation(0.15, 4, "X")
        me.transform(mw)
        shade_smooth(ob)
        me.materials.append(mat)
        objs.append(ob)
    return objs


def build_body_skin(mat, shirt_mat=None):
    """Neck + upper chest skin (only visible at the neckline)."""
    n = [
        (V(0, -0.005, 1.17), (0.140, 0.105)),
        (V(0, 0.002, 1.27), (0.125, 0.082)),
        (V(0, 0.010, 1.34), (0.068, 0.060)),
        (V(0, 0.014, 1.46), (0.054, 0.052)),
        (V(0.14, 0.008, 1.265), (0.048, 0.048)),
        (V(-0.14, 0.008, 1.265), (0.048, 0.048)),
    ]
    e = [(0, 1), (1, 2), (2, 3), (1, 4), (1, 5)]
    ob = skin_mesh("BodySkin", n, e, mat=mat)
    if shirt_mat is not None:
        # below the neckline the body is never meant to show: colour it like the
        # shirt so seams between torso and sleeves never reveal skin
        ob.data.materials.append(shirt_mat)
        for poly in ob.data.polygons:
            c = poly.center
            r_neck = math.hypot(c.x, (c.y - 0.01) * 1.25)
            front = max(0.0, -(c.y - 0.01) / max(r_neck, 1e-4))
            if c.z < 1.345 - 0.050 * front ** 1.5 - 0.012 or r_neck > 0.112:
                poly.material_index = 1
    return ob


def arm_nodes(sx):
    return {
        "shoulder": V(sx * 0.165, 0.012, 1.245),
        "elbow": V(sx * 0.200, 0.024, 1.090),
        "cuff": V(sx * 0.214, 0.004, 0.965),
        "wrist": V(sx * 0.222, 0.000, 0.860),
        "knuckle": V(sx * 0.226, -0.004, 0.775),
    }


def join(objs, name):
    """Merge baked mesh objects (identity transforms) into one."""
    bm = bmesh.new()
    for o in objs:
        bm.from_mesh(o.data)
    me = bpy.data.meshes.new(name)
    bm.to_mesh(me)
    bm.free()
    mats = objs[0].data.materials[:]
    for o in objs:
        bpy.data.objects.remove(o)
    ob = link(bpy.data.objects.new(name, me))
    for m in mats:
        me.materials.append(m)
    shade_smooth(ob)
    return ob


def build_shirt(mat):
    torso = skin_mesh("ShirtTorso", [
        (V(0, 0.002, 0.860), (0.188, 0.136)),   # hem (cut at 0.928)
        (V(0, -0.004, 1.030), (0.178, 0.128)),  # belly
        (V(0, -0.010, 1.160), (0.165, 0.124)),  # chest
        (V(0, 0.002, 1.245), (0.128, 0.094)),   # upper chest
        (V(0, 0.010, 1.345), (0.074, 0.066)),   # neck (cut away)
    ], [(0, 1), (1, 2), (2, 3), (3, 4)], subdiv=2)
    bm = bmesh.new()
    bm.from_mesh(torso.data)
    kill = []
    for f in bm.faces:
        c = f.calc_center_median()
        r_neck = math.hypot(c.x, (c.y - 0.01) * 1.25)
        front = max(0.0, -(c.y - 0.01) / max(r_neck, 1e-4))
        zcut = 1.345 - 0.050 * front ** 1.5
        if (c.z > zcut and r_neck < 0.108) or c.z < 0.928:
            kill.append(f)
    bmesh.ops.delete(bm, geom=kill, context="FACES")
    for v in bm.verts:   # snap the cut edges onto smooth curves
        if v.is_boundary:
            if v.co.z < 1.0:
                v.co.z = 0.928
            else:
                r_neck = math.hypot(v.co.x, (v.co.y - 0.01) * 1.25)
                front = max(0.0, -(v.co.y - 0.01) / max(r_neck, 1e-4))
                v.co.z = 0.5 * v.co.z + 0.5 * (1.345 - 0.050 * front ** 1.5)
    bm.to_mesh(torso.data)
    bm.free()
    parts = [torso]
    for sx in (1, -1):
        a = arm_nodes(sx)
        sl = skin_mesh("Sleeve", [
            (V(sx * 0.068, 0.008, 1.298), (0.048, 0.046)),   # starts inside the torso
            (a["shoulder"], (0.056, 0.056)),
            (a["elbow"], (0.050, 0.050)),
            (a["elbow"].lerp(a["cuff"], 0.55) + V(sx * 0.003, 0, 0), (0.054, 0.054)),  # bunch
            (a["cuff"], (0.050, 0.050)),
            (a["cuff"] - V(0, 0, 0.050), (0.046, 0.046)),
        ], [(0, 1), (1, 2), (2, 3), (3, 4), (4, 5)], subdiv=2)
        bm = bmesh.new()
        bm.from_mesh(sl.data)
        kill = [f for f in bm.faces if f.calc_center_median().z < a["cuff"].z - 0.008]
        bmesh.ops.delete(bm, geom=kill, context="FACES")
        for v in bm.verts:
            if v.is_boundary:
                v.co.z = a["cuff"].z - 0.008
        bm.to_mesh(sl.data)
        bm.free()
        parts.append(sl)
    for o in parts:
        so = o.modifiers.new("Solidify", "SOLIDIFY")
        so.thickness = 0.006
        so.offset = 1.0
        bake(o)
    ob = join(parts, "Shirt")
    ob.data.materials.append(mat)
    return ob


def build_pants(mat):
    n = [
        (V(0, 0.004, 0.975), (0.138, 0.104)),    # 0 waist
        (V(0, 0.012, 0.870), (0.146, 0.114)),    # 1 hips
    ]
    e = [(0, 1)]
    for sx in (1, -1):
        i = len(n)
        n += [
            (V(sx * 0.082, 0.010, 0.770), (0.086, 0.104)),
            (V(sx * 0.100, 0.004, 0.510), (0.070, 0.078)),
            (V(sx * 0.114, 0.012, 0.260), (0.066, 0.072)),
            (V(sx * 0.121, 0.018, 0.165), (0.076, 0.078)),  # cuff bunch
            (V(sx * 0.124, 0.022, 0.115), (0.070, 0.074)),
            (V(sx * 0.124, 0.022, 0.085), (0.060, 0.064)),  # cuff (cut)
        ]
        e += [(1, i), (i, i + 1), (i + 1, i + 2), (i + 2, i + 3), (i + 3, i + 4), (i + 4, i + 5)]
    ob = skin_mesh("Pants", n, e, subdiv=2, mat=mat)
    bm = bmesh.new()
    bm.from_mesh(ob.data)
    kill = [f for f in bm.faces if f.calc_center_median().z < 0.092]
    bmesh.ops.delete(bm, geom=kill, context="FACES")
    for v in bm.verts:
        if v.is_boundary:
            v.co.z = 0.092
    bm.to_mesh(ob.data)
    bm.free()
    so = ob.modifiers.new("Solidify", "SOLIDIFY")
    so.thickness = 0.005
    so.offset = 1.0
    bake(ob)
    shade_smooth(ob)
    cylinder_uv(ob, 0.80)
    return ob


def cylinder_uv(ob, split_z, tile=0.105):
    """Plaid UVs: unwrap each leg (and the pelvis) around its own vertical axis
    so the checks stay square and vertical like real fabric."""
    me = ob.data
    uv = me.uv_layers.new(name="UVMap")
    for poly in me.polygons:
        c = poly.center
        if c.z < split_z:
            ax = V(0.10 if c.x > 0 else -0.10, 0.012, 0)
            seam = 0.0   # seam on the inner side of the leg
            sgn = 1 if c.x > 0 else -1
        else:
            ax, seam, sgn = V(0, 0.01, 0), 0.0, 1
        us = []
        for li in poly.loop_indices:
            co = me.vertices[me.loops[li].vertex_index].co
            dx, dy = (co.x - ax.x) * sgn, co.y - ax.y
            if c.z < split_z:
                ang = math.atan2(dy, -dx)            # 0 = inner side
            else:
                ang = math.atan2(dx, dy)             # 0 = back centre
            r = 0.09 if c.z < split_z else 0.15
            us.append((ang, co.z, r))
        base = us[0][0]
        for li, (ang, z, r) in zip(poly.loop_indices, us):
            while ang - base > math.pi:
                ang -= 2 * math.pi
            while ang - base < -math.pi:
                ang += 2 * math.pi
            uv.data[li].uv = (ang * r / tile / 4, z / tile / 4)


def build_hands(mat):
    objs = []
    for sx in (1, -1):
        a = arm_nodes(sx)
        w, k = a["wrist"], a["knuckle"]
        palm = w.lerp(k, 0.5)
        n = [
            (a["cuff"] + V(0, 0.004, 0.03), (0.038, 0.038)),   # 0 forearm inside sleeve
            (w + V(0, 0, 0.04), (0.032, 0.034)),                # 1
            (w, (0.024, 0.031)),                               # 2 wrist
            (palm, (0.021, 0.038)),                            # 3 palm
            (k, (0.017, 0.038)),                               # 4 knuckles
        ]
        e = [(0, 1), (1, 2), (2, 3), (3, 4)]
        # fingers spread front-to-back along Y, curl toward the thigh (-sx)
        fingers = [(-0.027, 0.080, 0.0085), (-0.009, 0.088, 0.0087), (0.009, 0.083, 0.0083), (0.027, 0.068, 0.0075)]
        for fy, ln, r in fingers:
            base = k + V(0, fy, 0.004)
            i = len(n)
            p1 = base + V(-sx * 0.004, 0, -ln * 0.40)
            p2 = p1 + V(-sx * 0.007, 0, -ln * 0.32)
            p3 = p2 + V(-sx * 0.009, 0, -ln * 0.25)
            n += [(base, (r * 1.1, r * 1.1)), (p1, (r, r)), (p2, (r * 0.92, r * 0.92)), (p3, (r * 0.8, r * 0.8))]
            e += [(4, i), (i, i + 1), (i + 1, i + 2), (i + 2, i + 3)]
        # thumb (forward, -Y)
        i = len(n)
        t0 = palm + V(-sx * 0.006, -0.026, 0.012)
        t1 = t0 + V(-sx * 0.006, -0.014, -0.024)
        t2 = t1 + V(-sx * 0.006, -0.006, -0.024)
        n += [(t0, (0.011, 0.011)), (t1, (0.0088, 0.0088)), (t2, (0.0075, 0.0075))]
        e += [(3, i), (i, i + 1), (i + 1, i + 2)]
        ob = skin_mesh("Hand_L" if sx > 0 else "Hand_R", n, e, subdiv=2, mat=mat)
        objs.append(ob)
    return objs


def build_feet(mat):
    objs = []
    for sx in (1, -1):
        ax = sx * 0.124
        yaw = Matrix.Rotation(-sx * 0.12, 4, "Z")   # toes slightly outward

        def F(x, y, z):
            p = yaw @ Vector((x * sx * 1.12, y * 1.12, z))
            return V(ax + p.x, 0.030 + p.y, z)

        n = [
            (F(0, 0, 0.170), (0.034, 0.036)),     # 0 shin (inside pants)
            (F(0, 0, 0.075), (0.033, 0.036)),     # 1 ankle
            (F(0, 0.030, 0.034), (0.034, 0.030)),  # 2 heel
            (F(0.002, -0.040, 0.034), (0.040, 0.026)),  # 3 midfoot
            (F(0.004, -0.105, 0.022), (0.046, 0.020)),  # 4 ball
        ]
        e = [(0, 1), (1, 2), (1, 3), (3, 4)]
        toes = [(-0.026, 0.036, 0.0115), (-0.009, 0.030, 0.0088), (0.005, 0.027, 0.0082),
                (0.018, 0.024, 0.0076), (0.030, 0.019, 0.0070)]
        for tx, ln, r in toes:
            i = len(n)
            b = F(tx, -0.118, 0.020)
            t = F(tx * 1.08, -0.118 - ln, 0.014)
            n += [(b, (r * 1.15, r)), (t, (r, r * 0.9))]
            e += [(4, i), (i, i + 1)]
        ob = skin_mesh("Foot_L" if sx > 0 else "Foot_R", n, e, subdiv=2, mat=mat)
        # flatten the sole
        for v in ob.data.vertices:
            if v.co.z < 0.004:
                v.co.z = 0.004 + (v.co.z - 0.004) * 0.15
        objs.append(ob)
    return objs


def build_drawstring(mat):
    g = GeoBatch()
    front = lambda x, z: V(x, -0.118 - 0.012 * (1 - (x / 0.05) ** 2), z)
    knot = V(0.0, -0.132, 0.955)
    # loops of the bow
    for sx in (-1, 1):
        ctrl = [knot, knot + V(sx * 0.020, -0.004, 0.012), knot + V(sx * 0.040, -0.006, 0.006),
                knot + V(sx * 0.034, -0.004, -0.010), knot + V(sx * 0.006, -0.002, -0.002)]
        g.add(*sweep_geo(catmull(ctrl, 6), 0.0045, 0.0030, ring=6))
        # hanging tails
        ctrl = [knot, knot + V(sx * 0.008, -0.003, -0.020), knot + V(sx * 0.010, -0.002, -0.045),
                knot + V(sx * 0.006, -0.000, -0.070)]
        g.add(*sweep_geo(catmull(ctrl, 6), lambda t: 0.0036, 0.0028, ring=6))
        # aglet / end knot
        g.add(*sweep_geo([knot + V(sx * 0.006, 0.0, -0.068), knot + V(sx * 0.006, 0.0, -0.078)], 0.0048, 0.0048, ring=8))
    # knot
    g.add(*sweep_geo(catmull([knot + V(-0.006, 0, 0), knot + V(0, -0.004, 0), knot + V(0.006, 0, 0)], 4),
                     0.0065, 0.0055, ring=8))
    # eyelets
    return g.build("Drawstring", mat)


# --------------------------------------------------------------------------
# Hair
# --------------------------------------------------------------------------
BUN_C = V(0.0, 0.078, 1.668)
BUN_R = 0.100


def hairline_z(phi):
    """z of the hairline as a function of azimuth (0 = front, pi = back)."""
    a = abs(phi)
    table = [(0.0, 1.598), (0.45, 1.592), (0.9, 1.566), (1.25, 1.520), (1.55, 1.505), (1.85, 1.470),
             (2.4, 1.440), (math.pi, 1.432)]
    for (a0, z0), (a1, z1) in zip(table, table[1:]):
        if a <= a1:
            t = (a - a0) / (a1 - a0)
            t = t * t * (3 - 2 * t)
            return HEAD_C.z + (z0 + (z1 - z0) * t - 1.522) * KZ
    return HEAD_C.z + (table[-1][1] - 1.522) * KZ


def H(x, y, z):
    """Map a point authored for the 0.090 x 0.104 head onto the real head."""
    return V(x * KX, HEAD_C.y + (y + 0.012) * KX, HEAD_C.z + (z - 1.522) * KZ)


def head_surface(bvh, direction):
    hit = bvh.ray_cast(HEAD_C + direction * 0.3, -direction)
    if hit[0] is None:
        return HEAD_C + direction * 0.1, direction
    return hit[0], hit[1]


def build_hair(head, mat, mat_dark):
    me = head.data
    bvh = BVHTree.FromPolygons([v.co.copy() for v in me.vertices], [tuple(p.vertices) for p in me.polygons])

    # 1) scalp cap: head surface above the hairline, pushed out a bit
    bm = bmesh.new()
    bm.from_mesh(me)
    kill = []
    for f in bm.faces:
        c = f.calc_center_median()
        phi = math.atan2(c.x, -(c.y - HEAD_C.y))
        if c.z < hairline_z(phi):
            kill.append(f)
    bmesh.ops.delete(bm, geom=kill, context="FACES")
    for v in bm.verts:
        d = (v.co - HEAD_C).normalized()
        v.co += d * 0.009
    cap_me = bpy.data.meshes.new("HairCap")
    bm.to_mesh(cap_me)
    bm.free()
    cap = link(bpy.data.objects.new("HairCap", cap_me))
    cap_me.uv_layers.clear() if hasattr(cap_me.uv_layers, "clear") else None
    cap_me.materials.clear()
    cap_me.materials.append(mat_dark)
    so = cap.modifiers.new("Solidify", "SOLIDIFY")
    so.thickness = 0.006
    so.offset = -1
    bake(cap)
    shade_smooth(cap)

    g = GeoBatch()
    gd = GeoBatch()

    # 2) combed-up strands from the hairline to the bun
    bun_base = BUN_C + V(0, -0.02, -0.055)
    count = 96
    for i in range(count):
        phi = -math.pi + 2 * math.pi * (i + RNG.random() * 0.6) / count
        z0 = hairline_z(phi) + 0.004
        # start direction on the head at that azimuth/height
        d0 = Vector((math.sin(phi), -math.cos(phi), 0))
        dz = (z0 - HEAD_C.z) / HEAD_R.z
        d0 = Vector((d0.x * math.sqrt(max(0, 1 - dz * dz)), d0.y * math.sqrt(max(0, 1 - dz * dz)), dz)).normalized()
        target = (bun_base + V(RNG.uniform(-0.03, 0.03), RNG.uniform(-0.02, 0.02), RNG.uniform(-0.01, 0.02)) - HEAD_C).normalized()
        pts, nrm = [], []
        steps = 14
        for k in range(steps + 1):
            t = k / steps
            dvec = d0.slerp(target, t) if d0.dot(target) > -0.99 else d0
            p, nn = head_surface(bvh, dvec)
            lift = 0.004 + 0.012 * math.sin(math.pi * t) ** 0.7 + 0.003 * RNG.random()
            pts.append(p + nn * lift)
            nrm.append(nn)
        pts = catmull(pts[::2], 4)
        width = lambda t, w=RNG.uniform(0.010, 0.014): w * (0.35 + 0.65 * math.sin(math.pi * min(1, t * 1.1 + 0.05)))
        (gd if i % 5 == 0 else g).add(*sweep_geo(pts, width, 0.0040,
                                                 up=lambda i_, p_: (p_ - HEAD_C).normalized(), ring=6))

    # 3) the messy bun: core + big twisted loops + flyaways
    bm = bmesh.new()
    bmesh.ops.create_uvsphere(bm, u_segments=24, v_segments=16, radius=BUN_R * 0.86)
    bmesh.ops.translate(bm, verts=bm.verts, vec=BUN_C)
    core_me = bpy.data.meshes.new("BunCore")
    bm.to_mesh(core_me)
    bm.free()
    core = link(bpy.data.objects.new("BunCore", core_me))
    tx = bpy.data.textures.new("BunNoise", "CLOUDS")
    tx.noise_scale = 0.35
    sd = core.modifiers.new("Sub", "SUBSURF")
    sd.levels = 2
    dm = core.modifiers.new("Disp", "DISPLACE")
    dm.texture = tx
    dm.strength = 0.045
    dm.mid_level = 0.5
    bake(core)
    shade_smooth(core)
    core.data.materials.append(mat_dark)

    bun_up = (BUN_C - HEAD_C).normalized()
    for i in range(12):
        # random loop plane, biased so loops wrap around the bun
        axis = Vector((RNG.uniform(-1, 1), RNG.uniform(-1, 1), RNG.uniform(-1, 1))).normalized()
        axis = (axis + bun_up * RNG.uniform(0.0, 1.2)).normalized()
        u = axis.orthogonal().normalized()
        v = axis.cross(u).normalized()
        rad = BUN_R * RNG.uniform(0.55, 0.95)
        off = Vector((RNG.uniform(-1, 1), RNG.uniform(-1, 1), RNG.uniform(-0.6, 1))) * 0.02
        span = RNG.uniform(1.2, 1.9) * math.pi
        a0 = RNG.uniform(0, 2 * math.pi)
        ctrl = []
        for k in range(9):
            a = a0 + span * k / 8
            wob = 1 + 0.12 * math.sin(a * 3 + i)
            ctrl.append(BUN_C + off + (u * math.cos(a) + v * math.sin(a)) * rad * wob)
        pts = catmull(ctrl, 5)
        w0 = RNG.uniform(0.018, 0.026)
        width = lambda t, w=w0: w * (0.35 + 0.65 * math.sin(math.pi * t) ** 0.6)
        (g if i % 3 else gd).add(*sweep_geo(pts, width, lambda t, w=w0: w * 0.40 * (0.4 + 0.6 * math.sin(math.pi * t) ** 0.6),
                                            up=lambda i_, p_: (p_ - BUN_C).normalized(), ring=8))
    # many small coiled curls over the bun surface -> curly texture
    for i in range(40):
        d = Vector((RNG.gauss(0, 1), RNG.gauss(0, 1), RNG.gauss(0, 1))).normalized()
        if d.dot(bun_up) < -0.55:
            continue
        tdir = d.orthogonal().normalized()
        tdir = (Matrix.Rotation(RNG.uniform(0, 6.28), 3, d) @ tdir).normalized()
        length = RNG.uniform(0.06, 0.11)
        hel = RNG.uniform(0.010, 0.016)
        turns = RNG.uniform(1.2, 2.2)
        pts = []
        for k in range(29):
            t = k / 28
            base_dir = (d + tdir * (t - 0.5) * length / BUN_R).normalized()
            c0 = BUN_C + base_dir * BUN_R * RNG.uniform(0.97, 1.0)
            side = base_dir.cross(tdir).normalized()
            a = 2 * math.pi * turns * t
            pts.append(c0 + (side * math.cos(a) + base_dir * math.sin(a)) * hel)
        w0 = RNG.uniform(0.012, 0.016)
        (gd if i % 4 == 0 else g).add(*sweep_geo(pts, lambda t, w=w0: w * (0.4 + 0.6 * math.sin(math.pi * t) ** 0.5),
                                                 lambda t, w=w0: w * 0.45 * (0.4 + 0.6 * math.sin(math.pi * t) ** 0.5), ring=6))
    # flyaway curls from the bun
    for i in range(12):
        d = Vector((RNG.uniform(-1, 1), RNG.uniform(-1, 1), RNG.uniform(-0.3, 1))).normalized()
        start = BUN_C + d * BUN_R * 0.8
        side = d.orthogonal().normalized()
        ctrl = [start]
        for k in range(1, 5):
            ctrl.append(start + d * 0.018 * k + side * 0.012 * math.sin(k * 1.7) + V(0, 0, -0.004 * k * k))
        g.add(*sweep_geo(catmull(ctrl, 5), lambda t: 0.0045 * (1 - t) + 0.0008, lambda t: 0.0025 * (1 - t) + 0.0006, ring=5))

    # 4) loose tendrils framing the face and at the nape
    def tendril(start, drop, out, fwd, waves, w, target):
        ctrl = []
        n = 7
        for k in range(n):
            t = k / (n - 1)
            side = out * t + 0.006 * math.sin(t * math.pi * waves)
            p = start + V(side, fwd * t + 0.004 * math.cos(t * math.pi * waves), -drop * t)
            ctrl.append(p)
        pts = catmull(ctrl, 5)
        target.add(*sweep_geo(pts, lambda t: w * (1 - 0.85 * t), lambda t: w * 0.45 * (1 - 0.8 * t), ring=6))

    for sx in (-1, 1):
        # long face-framing strands in front of the ears
        tendril(H(sx * 0.074, -0.058, 1.585), 0.150, sx * 0.020, -0.010, 3.0, 0.0085, g)
        tendril(H(sx * 0.082, -0.040, 1.570), 0.130, sx * 0.024, 0.004, 2.5, 0.0075, gd)
        tendril(H(sx * 0.066, -0.070, 1.596), 0.090, sx * 0.018, -0.006, 2.0, 0.0065, g)
        tendril(H(sx * 0.088, -0.018, 1.555), 0.110, sx * 0.016, 0.012, 3.5, 0.0070, g)
        # curtain bang sweeping off the forehead
        ctrl = [H(sx * 0.010, -0.098, 1.610), H(sx * 0.032, -0.100, 1.596), H(sx * 0.056, -0.088, 1.582),
                H(sx * 0.074, -0.068, 1.560), H(sx * 0.086, -0.050, 1.530)]
        g.add(*sweep_geo(catmull(ctrl, 5), lambda t: 0.011 * (1 - 0.7 * t), lambda t: 0.004 * (1 - 0.6 * t),
                         up=lambda i_, p_: (p_ - HEAD_C).normalized(), ring=6))
        # hair volume over the ears
        for k in range(3):
            ctrl = [H(sx * 0.080, 0.00 + 0.02 * k, 1.585 - 0.01 * k), H(sx * 0.097, 0.01 + 0.02 * k, 1.545),
                    H(sx * 0.097, 0.03 + 0.02 * k, 1.505), H(sx * 0.086, 0.06 + 0.01 * k, 1.480)]
            (g if k % 2 else gd).add(*sweep_geo(catmull(ctrl, 5), lambda t: 0.014 * (1 - 0.5 * t), 0.006,
                                                up=lambda i_, p_: (p_ - HEAD_C).normalized(), ring=6))
    # nape wisps
    for i, x in enumerate((-0.05, -0.028, -0.006, 0.018, 0.04, 0.06)):
        tendril(H(x, 0.072, 1.462), 0.050 + 0.012 * (i % 3), 0.006 * math.sin(i * 2.1), 0.012, 2.0, 0.006,
                g if i % 2 else gd)

    hair = g.build("HairStrands", mat)
    hair2 = gd.build("HairStrandsDark", mat_dark)
    return [cap, core, hair, hair2]


# --------------------------------------------------------------------------
# Rig + skinning
# --------------------------------------------------------------------------
BONES = [
    # name, head, tail, parent
    ("hips", V(0, 0.008, 0.880), V(0, 0.008, 0.990), None),
    ("spine", V(0, 0.008, 0.990), V(0, 0.004, 1.120), "hips"),
    ("chest", V(0, 0.004, 1.120), V(0, 0.006, 1.300), "spine"),
    ("neck", V(0, 0.008, 1.340), V(0, 0.000, 1.450), "chest"),
    ("head", V(0, 0.000, 1.450), V(0, -0.010, 1.640), "neck"),
]
for sx, s in ((1, "L"), (-1, "R")):
    a = arm_nodes(sx)
    BONES += [
        (f"shoulder.{s}", V(sx * 0.030, 0.010, 1.300), a["shoulder"], "chest"),
        (f"upper_arm.{s}", a["shoulder"], a["elbow"], f"shoulder.{s}"),
        (f"forearm.{s}", a["elbow"], a["wrist"], f"upper_arm.{s}"),
        (f"hand.{s}", a["wrist"], a["knuckle"] + V(0, 0, -0.06), f"forearm.{s}"),
        (f"thigh.{s}", V(sx * 0.090, 0.010, 0.840), V(sx * 0.102, 0.004, 0.500), "hips"),
        (f"shin.{s}", V(sx * 0.102, 0.004, 0.500), V(sx * 0.124, 0.030, 0.075), f"thigh.{s}"),
        (f"foot.{s}", V(sx * 0.124, 0.030, 0.075), V(sx * 0.136, -0.080, 0.022), f"shin.{s}"),
        (f"toe.{s}", V(sx * 0.136, -0.080, 0.022), V(sx * 0.142, -0.150, 0.018), f"foot.{s}"),
    ]


def build_armature():
    arm = bpy.data.armatures.new("Rig")
    ob = link(bpy.data.objects.new("Rig", arm))
    bpy.context.view_layer.objects.active = ob
    ob.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    eb = {}
    for name, h, t, parent in BONES:
        b = arm.edit_bones.new(name)
        b.head, b.tail = h, t
        b.roll = 0
        if parent:
            b.parent = eb[parent]
            b.use_connect = (eb[parent].tail - h).length < 1e-4
        eb[name] = b
    bpy.ops.object.mode_set(mode="OBJECT")
    arm.display_type = "STICK"
    return ob


def seg_dist(p, a, b):
    ab = b - a
    t = max(0.0, min(1.0, (p - a).dot(ab) / max(ab.length_squared, 1e-9)))
    return (p - (a + ab * t)).length


def skin_to(ob, rig, bones, power=5.0, rigid=None):
    """Distance-to-bone weights limited to `bones` (or rigid to one bone)."""
    ob.parent = rig
    mod = ob.modifiers.new("Armature", "ARMATURE")
    mod.object = rig
    groups = {b: ob.vertex_groups.new(name=b) for b in ([rigid] if rigid else bones)}
    if rigid:
        groups[rigid].add(range(len(ob.data.vertices)), 1.0, "REPLACE")
        return
    segs = {name: (h, t) for name, h, t, _ in BONES if name in bones}
    for v in ob.data.vertices:
        ws = []
        for b, (h, t) in segs.items():
            d = seg_dist(v.co, h, t) + 0.01
            ws.append((1.0 / d ** power, b))
        ws.sort(reverse=True)
        ws = ws[:3]
        tot = sum(w for w, _ in ws)
        for w, b in ws:
            if w / tot > 0.02:
                groups[b].add([v.index], w / tot, "REPLACE")


# --------------------------------------------------------------------------
# Animations
# --------------------------------------------------------------------------
def key_pose(rig, frame, rots, loc=None):
    for pb in rig.pose.bones:
        e = rots.get(pb.name, (0, 0, 0))
        pb.rotation_mode = "XYZ"
        pb.rotation_euler = e
        pb.keyframe_insert("rotation_euler", frame=frame)
    if loc is not None:
        pb = rig.pose.bones["hips"]
        pb.location = loc
        pb.keyframe_insert("location", frame=frame)


def make_action(rig, name):
    act = bpy.data.actions.new(name)
    act.use_fake_user = True
    rig.animation_data_create()
    rig.animation_data.action = act
    return act


def build_animations(rig):
    """Bone-local rotations (X = bend forward for limbs pointing down)."""
    arms_down = {"upper_arm.L": (0, 0, 0), "upper_arm.R": (0, 0, 0)}
    # Idle: 2s breathing + subtle sway
    make_action(rig, "Idle")
    for f in range(0, 61, 10):
        s = math.sin(2 * math.pi * f / 60)
        rots = dict(arms_down)
        rots["chest"] = (0.03 * s, 0, 0)
        rots["neck"] = (-0.02 * s, 0.03 * s, 0)
        rots["head"] = (0.02 * s, -0.04 * s, 0.02 * s)
        rots["upper_arm.L"] = (0.03 * s, 0, 0)
        rots["upper_arm.R"] = (0.03 * s, 0, 0)
        rots["forearm.L"] = (0.06 + 0.02 * s, 0, 0)
        rots["forearm.R"] = (0.06 + 0.02 * s, 0, 0)
        key_pose(rig, f + 1, rots, loc=(0, 0.004 * s, 0))
    # Walk: 1s cycle
    make_action(rig, "Walk")
    for f in range(0, 31, 3):
        ph = 2 * math.pi * f / 30
        s, c = math.sin(ph), math.cos(ph)
        rots = {}
        for side, sg in (("L", 1), ("R", -1)):
            sw = s * sg
            rots[f"thigh.{side}"] = (-0.45 * sw, 0, 0)
            knee = max(0.0, math.sin(ph * 1 + (0 if sg > 0 else math.pi) + 1.2))
            rots[f"shin.{side}"] = (0.65 * knee, 0, 0)
            rots[f"foot.{side}"] = (0.15 * sw, 0, 0)
            rots[f"upper_arm.{side}"] = (0.35 * sw, 0, 0)
            rots[f"forearm.{side}"] = (0.25 + 0.15 * max(0, sw), 0, 0)
        rots["spine"] = (0.03, 0.06 * s, 0)
        rots["chest"] = (0, -0.1 * s, 0)
        rots["head"] = (0, 0.05 * s, 0)
        key_pose(rig, f + 1, rots, loc=(0, 0.018 * abs(c) - 0.01, 0))
    rig.animation_data.action = bpy.data.actions["Idle"]
    # push both actions on NLA tracks so the glTF exporter keeps them
    for act in bpy.data.actions:
        tr = rig.animation_data.nla_tracks.new()
        tr.name = act.name
        tr.strips.new(act.name, 1, act)
        tr.mute = True
    rig.animation_data.action = None
    for pb in rig.pose.bones:
        pb.rotation_euler = (0, 0, 0)
        pb.location = (0, 0, 0)


# --------------------------------------------------------------------------
# Render turnaround
# --------------------------------------------------------------------------
def setup_render(res_x=420, res_y=900, samples=48):
    sc = bpy.context.scene
    sc.render.engine = "CYCLES"
    sc.cycles.device = "CPU"
    sc.cycles.samples = samples
    sc.cycles.use_denoising = True
    sc.render.film_transparent = True
    sc.render.resolution_x, sc.render.resolution_y = res_x, res_y
    sc.view_settings.view_transform = "Standard"
    world = bpy.data.worlds.new("World")
    sc.world = world
    world.use_nodes = True
    world.node_tree.nodes["Background"].inputs[0].default_value = (0.85, 0.87, 0.92, 1)
    world.node_tree.nodes["Background"].inputs[1].default_value = 0.7
    for name, rot, energy in (("Key", (0.9, 0.0, -0.6), 2.4), ("Rim", (1.1, 0, 2.6), 1.6)):
        L = bpy.data.lights.new(name, "SUN")
        L.energy = energy
        L.angle = 0.3
        lo = link(bpy.data.objects.new(name, L))
        lo.rotation_euler = rot
    cam = bpy.data.cameras.new("Cam")
    cam.type = "ORTHO"
    cam.ortho_scale = 1.9
    co = link(bpy.data.objects.new("Cam", cam))
    sc.camera = co
    return co


def render_views(cam, prefix="view"):
    # azimuths matching the reference sheet: front, 3/4, profile, back, back 3/4
    views = [("front", 0), ("threequarter", -40), ("side", -90), ("back", 180), ("back_threequarter", -140)]
    paths = []
    for name, az in views:
        a = math.radians(az)
        d = 4.0
        pos = V(math.sin(a) * d, -math.cos(a) * d, 0.93)
        cam.location = pos
        cam.rotation_euler = (math.pi / 2, 0, a)
        p = os.path.join(OUT_RENDER, f"{prefix}_{name}.png")
        bpy.context.scene.render.filepath = p
        bpy.ops.render.render(write_still=True)
        paths.append(p)
    return paths


def compose_sheet(paths, out, ref=None):
    from PIL import Image
    ims = [Image.open(p).convert("RGBA") for p in paths]
    w, h = ims[0].size
    sheet = Image.new("RGBA", (w * len(ims), h), (255, 255, 255, 255))
    for i, im in enumerate(ims):
        sheet.alpha_composite(im, (i * w, 0))
    if ref:
        r = Image.open(ref).convert("RGBA")
        bg = Image.new("RGBA", r.size, (255, 255, 255, 255))
        bg.alpha_composite(r)
        r = bg.resize((sheet.width, int(r.height * sheet.width / r.width)))
        both = Image.new("RGBA", (sheet.width, sheet.height + r.height), (255, 255, 255, 255))
        both.alpha_composite(r, (0, 0))
        both.alpha_composite(sheet, (0, r.height))
        both.convert("RGB").save(out.replace(".png", "_vs_reference.png"))
    sheet.convert("RGB").save(out)


def glb_to_embedded_gltf(glb_path, out_path):
    """Single-file .gltf (buffer as base64 data URI) for web hosts that don't serve .glb."""
    import base64
    import json
    import struct
    data = open(glb_path, "rb").read()
    jlen = struct.unpack_from("<I", data, 12)[0]
    doc = json.loads(data[20:20 + jlen])
    blen = struct.unpack_from("<I", data, 20 + jlen)[0]
    binchunk = data[28 + jlen:28 + jlen + blen]
    doc["buffers"][0]["uri"] = "data:application/octet-stream;base64," + base64.b64encode(binchunk).decode()
    with open(out_path, "w") as f:
        json.dump(doc, f, separators=(",", ":"))


# --------------------------------------------------------------------------
def main():
    render = "--no-render" not in sys.argv
    reset()
    face_img = face_texture()
    plaid_img = plaid_texture()
    m_skin = material("Skin", COL["skin"], rough=0.6)
    m_face = material("Face", COL["skin"], rough=0.6, image=face_img)
    m_shirt = material("Shirt", COL["shirt"], rough=0.95, spec=0.1)
    m_pants = material("Pants", COL["pants"], rough=0.95, image=plaid_img, spec=0.1)
    m_hair = material("Hair", COL["hair"], rough=0.55, spec=0.4)
    m_hair_dark = material("HairDark", COL["hair_dark"], rough=0.6, spec=0.3)
    m_string = material("Drawstring", COL["string"], rough=0.9)

    head = build_head(m_face)
    ears = build_ears(m_skin)
    body = build_body_skin(m_skin, m_shirt)
    shirt = build_shirt(m_shirt)
    pants = build_pants(m_pants)
    hands = build_hands(m_skin)
    feet = build_feet(m_skin)
    string = build_drawstring(m_string)
    hair = build_hair(head, m_hair, m_hair_dark)

    rig = build_armature()
    torso = ["hips", "spine", "chest", "neck", "shoulder.L", "shoulder.R",
             "upper_arm.L", "upper_arm.R", "forearm.L", "forearm.R"]
    skin_to(shirt, rig, torso)
    skin_to(body, rig, ["chest", "neck", "head", "shoulder.L", "shoulder.R"])
    skin_to(pants, rig, ["hips", "spine", "thigh.L", "thigh.R", "shin.L", "shin.R"])
    skin_to(string, rig, None, rigid="hips")
    for h, s in zip(hands, "LR"):
        skin_to(h, rig, [f"forearm.{s}", f"hand.{s}"], power=6)
    for f, s in zip(feet, "LR"):
        skin_to(f, rig, [f"shin.{s}", f"foot.{s}", f"toe.{s}"], power=6)
    for o in [head] + ears + hair:
        skin_to(o, rig, None, rigid="head")
    build_animations(rig)

    blend = os.path.join(OUT_EXPORT, "character.blend")
    bpy.context.preferences.filepaths.save_version = 0
    bpy.ops.wm.save_as_mainfile(filepath=blend)
    glb = os.path.join(OUT_EXPORT, "character.glb")
    bpy.ops.export_scene.gltf(filepath=glb, export_format="GLB", export_yup=True,
                              export_animations=True, export_animation_mode="NLA_TRACKS",
                              export_skins=True, export_image_format="AUTO")
    glb_to_embedded_gltf(glb, os.path.join(OUT_EXPORT, "character.gltf"))
    tris = sum(len(p.vertices) - 2 for o in bpy.data.objects if o.type == "MESH" for p in o.data.polygons)
    print(f"EXPORTED {glb} triangles={tris}")

    if render:
        rig.data.pose_position = "REST"
        cam = setup_render()
        paths = render_views(cam)
        compose_sheet(paths, os.path.join(OUT_RENDER, "turnaround.png"),
                      ref=os.path.join(ROOT, "reference", "turnaround.png"))
        cam.data.ortho_scale = 0.42
        for name, az in (("face", 0), ("face_threequarter", -40), ("face_side", -90)):
            a = math.radians(az)
            cam.location = V(math.sin(a) * 4, -math.cos(a) * 4, 1.56)
            cam.rotation_euler = (math.pi / 2, 0, a)
            bpy.context.scene.render.resolution_x = bpy.context.scene.render.resolution_y = 600
            bpy.context.scene.render.filepath = os.path.join(OUT_RENDER, f"closeup_{name}.png")
            bpy.ops.render.render(write_still=True)
        compose_sheet([os.path.join(OUT_RENDER, f"closeup_{n}.png") for n in ("face", "face_threequarter", "face_side")],
                      os.path.join(OUT_RENDER, "closeups.png"))
        print("RENDERED", paths)


if __name__ == "__main__":
    main()
