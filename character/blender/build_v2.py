"""v2: the pajama character rebuilt from anatomy.

    python3 character/blender/build_v2.py              # build, export, render
    python3 character/blender/build_v2.py --no-render

Workflow (the usual one for stylized game characters):
  1. Body: anatomical sections (anatomy.py) lofted per part, unioned with a voxel
     remesh, joins blended, decimated. Proportions follow the research table in
     anatomy.py: real female landmarks (crotch at half height, elbow at the waist,
     hips as wide as the shoulders) with the anime changes (large head, thin long
     neck, slim limbs, small hands and feet).
  2. Head: centred, modelled from the drawn profile; anime face as one decal.
  3. Hair: its own mesh (continuous volume + strand clumps + bun).
  4. Clothes: built from the SAME skeleton paths and body sections plus ease, so
     they fit by construction, then cut open with clean planar cuts.
  5. Weights: bone heat on the body, then transferred to every garment
     (Data Transfer, nearest face), so fabric bends exactly like the skin under it.
"""
import math
import os
import sys

import bpy
import bmesh
import numpy as np
from mathutils import Vector, Matrix

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import head_from_drawing as HD  # noqa: E402
HD.XC = 0.0                     # centre the head on the body
import anatomy as A  # noqa: E402
import build_character as BC  # noqa: E402
import build_anime as AN  # noqa: E402
import body_test as BT  # noqa: E402

V = A.V
OUT_EXPORT, OUT_RENDER, OUT_TEX = BC.OUT_EXPORT, BC.OUT_RENDER, BC.OUT_TEX
ROOT = BC.ROOT


# ---------------------------------------------------------------- rig
def build_rig():
    arm = bpy.data.armatures.new("Rig")
    ob = BC.link(bpy.data.objects.new("Rig", arm))
    bpy.context.view_layer.objects.active = ob
    ob.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    J = A.joints()
    eb = {}
    for name in A.BONE_ORDER:
        h, t = J[name]
        b = arm.edit_bones.new(name)
        b.head, b.tail = h, t
        p = A.PARENT[name]
        if p:
            b.parent = eb[p]
            b.use_connect = (eb[p].tail - h).length < 1e-4
        eb[name] = b
    bpy.ops.object.mode_set(mode="OBJECT")
    arm.display_type = "STICK"
    return ob


def bone_heat(ob, rig):
    for o in bpy.context.view_layer.objects:
        o.select_set(False)
    ob.select_set(True)
    rig.select_set(True)
    bpy.context.view_layer.objects.active = rig
    bpy.ops.object.parent_set(type="ARMATURE_AUTO")
    empty = sum(1 for v in ob.data.vertices if not v.groups)
    print(f"  bone heat on {ob.name}: {empty} unweighted vertices")


def transfer_weights(src, dst, rig):
    """Standard garment skinning: copy vertex groups from the body's nearest surface."""
    for g in src.vertex_groups:
        if g.name not in dst.vertex_groups:
            dst.vertex_groups.new(name=g.name)
    m = dst.modifiers.new("Weights", "DATA_TRANSFER")
    m.object = src
    m.use_vert_data = True
    m.data_types_verts = {"VGROUP_WEIGHTS"}
    m.vert_mapping = "POLYINTERP_NEAREST"
    m.layers_vgroup_select_src = "ALL"
    m.layers_vgroup_select_dst = "NAME"
    for o in bpy.context.view_layer.objects:
        o.select_set(False)
    dst.select_set(True)
    bpy.context.view_layer.objects.active = dst
    bpy.ops.object.datalayout_transfer(modifier=m.name)
    bpy.ops.object.modifier_apply(modifier=m.name)
    dst.parent = rig
    am = dst.modifiers.new("Armature", "ARMATURE")
    am.object = rig


def restrict_weights(ob, allowed_fn):
    """Zero weights from bones that must not move this vertex, then renormalise."""
    names = {g.index: g.name for g in ob.vertex_groups}
    for v in ob.data.vertices:
        ws = {names[g.group]: g.weight for g in v.groups}
        keep = {b: w for b, w in ws.items() if allowed_fn(v.co, b)}
        tot = sum(keep.values())
        if tot <= 1e-6:
            # fall back to the nearest allowed trunk bone
            keep = {("hips" if v.co.z < 1.0 else "chest"): 1.0}
            tot = 1.0
        for b in ws:
            ob.vertex_groups[b].remove([v.index])
        for b, w in keep.items():
            g = ob.vertex_groups.get(b) or ob.vertex_groups.new(name=b)
            g.add([v.index], w / tot, "REPLACE")


def pants_allowed(co, bone):
    return bone.split(".")[0] in ("hips", "spine", "thigh", "shin", "foot")


def sweater_allowed(co, bone):
    base = bone.split(".")[0]
    if base in ("thigh", "shin", "foot", "toe", "hand", "head"):
        return False
    if base in ("forearm", "upper_arm"):
        sx = 1 if co.x > 0 else -1
        path = A.arm_path(sx)
        d = min((co - A._spline(path, t / 20)).length for t in range(21))
        return d < 0.075            # only the sleeve follows the arm
    return True


def rigid(ob, rig, bone="head"):
    ob.parent = rig
    g = ob.vertex_groups.new(name=bone)
    g.add(range(len(ob.data.vertices)), 1.0, "REPLACE")
    m = ob.modifiers.new("Armature", "ARMATURE")
    m.object = rig


# ---------------------------------------------------------------- clothes
def eased(table, ease_fn, z_range=None):
    out = []
    for row in table:
        z = row[0]
        if z_range and not (z_range[0] <= z <= z_range[1]):
            continue
        e = ease_fn(z)
        out.append((z, row[1] + e, row[2] + e, row[3] + e, row[4], row[5]))
    return out


def cut(ob, planes):
    """planes: list of (point, normal); removes everything on the normal side, clean edges."""
    bm = bmesh.new()
    bm.from_mesh(ob.data)
    for co, no in planes:
        geom = bm.verts[:] + bm.edges[:] + bm.faces[:]
        bmesh.ops.bisect_plane(bm, geom=geom, plane_co=co, plane_no=no, clear_outer=True, dist=1e-5)
    bmesh.ops.delete(bm, geom=[v for v in bm.verts if not v.link_faces], context="VERTS")
    bm.to_mesh(ob.data)
    bm.free()


def cut_region(ob, keep_fn):
    """Remove faces whose centre fails keep_fn (used where a single plane is not enough)."""
    bm = bmesh.new()
    bm.from_mesh(ob.data)
    bmesh.ops.delete(bm, geom=[f for f in bm.faces if not keep_fn(f.calc_center_median())], context="FACES")
    bmesh.ops.delete(bm, geom=[v for v in bm.verts if not v.link_faces], context="VERTS")
    bm.to_mesh(ob.data)
    bm.free()


def thicken(ob, t):
    so = ob.modifiers.new("Thick", "SOLIDIFY")
    so.thickness = t
    so.offset = 1.0
    so.use_rim = True
    so.use_even_offset = True
    BC.bake(ob)
    BC.shade_smooth(ob)


SLEEVE = [   # t, across, front, back, roundness (loose knit sleeve, bunched near the cuff)
    (0.00, 0.040, 0.040, 0.040, 2.0),
    (0.10, 0.058, 0.060, 0.058, 2.0),
    (0.30, 0.054, 0.054, 0.054, 2.0),
    (0.55, 0.050, 0.050, 0.052, 2.0),
    (0.68, 0.054, 0.054, 0.054, 2.0),   # pushed-up bunch
    (0.76, 0.052, 0.052, 0.052, 2.0),
    (0.84, 0.042, 0.040, 0.040, 2.0),   # ribbed cuff
    (0.90, 0.040, 0.038, 0.038, 2.0),
]
SLEEVE_END_T = 0.86


def sweater_parts():
    def ease(z):     # snug on the shoulders, a relaxed drop toward the hem
        return 0.012 + 0.022 * min(1.0, max(0.0, (1.12 - z) / 0.20)) ** 1.5
    torso = eased(A.TORSO, ease, (0.85, 1.4))
    torso = [(0.860, 0.180, 0.100, 0.118, 2.6, 0.010)] + torso[1:]
    parts = [A.vertical_loft(torso, rings=80, m=56)]
    for sx in (1, -1):
        parts.append(A.limb_loft(A.arm_path(sx), SLEEVE, sx, m=40))
    return parts


def sweater_weight(p):
    w = 0.0
    for sx in (1, -1):
        w = max(w, max(0.0, 1 - (p - V(sx * 0.13, 0.012, 1.285)).length / 0.09))
    return w ** 1.2


def build_sweater(mat):
    ob = BT.remesh(BT.union_body(sweater_parts(), "Outfit_Sweater"), voxel=0.003, faces=14000,
                   weight_fn=sweater_weight)
    planes = [(V(0, 0, 0.905), V(0, 0, -1))]          # hem
    for sx in (1, -1):
        path = A.arm_path(sx)
        c = A._spline(path, SLEEVE_END_T)
        tan = (A._spline(path, SLEEVE_END_T + 0.01) - A._spline(path, SLEEVE_END_T - 0.01)).normalized()
        planes.append((c, tan))                          # sleeve end, perpendicular to the forearm
    cut(ob, planes)

    # crew neckline: an ellipse around the neck, dipping a little at the front
    def keep(c):
        r = math.hypot(c.x / 0.090, (c.y - 0.010) / 0.075)
        zn = 1.352 - 0.020 * max(0.0, -(c.y - 0.01) / 0.075)
        return not (r < 1.0 and c.z > zn - 0.02) and c.z < 1.40
    cut_region(ob, keep)
    # snap the neckline onto its ellipse and relax it, so the collar edge is a clean curve
    bm = bmesh.new()
    bm.from_mesh(ob.data)
    ring = [v for v in bm.verts if v.is_boundary and v.co.z > 1.25]
    for _ in range(12):
        for v in ring:
            ang = math.atan2((v.co.y - 0.010) / 0.075, v.co.x / 0.090)
            x, y = 0.090 * math.cos(ang), 0.010 + 0.075 * math.sin(ang)
            zn = 1.352 - 0.020 * max(0.0, -(y - 0.01) / 0.075) - 0.02
            v.co = v.co.lerp(Vector((x, y, zn)), 0.5)
        new = {}
        for v in ring:
            nb = [e.other_vert(v) for e in v.link_edges if e.is_boundary]
            if len(nb) == 2:
                new[v] = (v.co * 2 + nb[0].co + nb[1].co) / 4
        for v, co in new.items():
            v.co = co
    # relax the band of faces next to the collar too
    band = {e.other_vert(v) for v in ring for e in v.link_edges} - set(ring)
    bmesh.ops.smooth_vert(bm, verts=list(band), factor=0.6, use_axis_x=True, use_axis_y=True, use_axis_z=True)
    bm.to_mesh(ob.data)
    bm.free()
    thicken(ob, 0.004)
    ob.data.materials.append(mat)
    return ob


PANT_LEG = [  # relaxed straight jogger leg, gathered into a cuff
    (0.00, 0.050, 0.050, 0.055, 2.0),
    (0.12, 0.094, 0.094, 0.104, 2.2),
    (0.25, 0.092, 0.090, 0.094, 2.2),
    (0.45, 0.080, 0.078, 0.080, 2.1),
    (0.60, 0.074, 0.070, 0.074, 2.0),
    (0.80, 0.072, 0.068, 0.072, 2.0),
    (0.90, 0.070, 0.066, 0.070, 2.0),   # fabric pooled above the cuff
    (0.95, 0.048, 0.046, 0.048, 2.0),   # elastic cuff
    (1.00, 0.045, 0.044, 0.046, 2.0),
]


def pants_parts():
    pelvis = eased(A.TORSO, lambda z: 0.014, (0.78, 1.07))
    parts = [A.vertical_loft(pelvis, rings=40, m=56)]
    for sx in (1, -1):
        parts.append(A.limb_loft(A.leg_path(sx), PANT_LEG, sx, m=40))
    return parts


def pants_weight(p):
    return max(0.0, 1 - abs(p.z - 0.84) / 0.09) ** 1.2 if abs(p.x) < 0.22 else 0.0


def build_pants(mat, cuff_z=0.098):
    ob = BT.remesh(BT.union_body(pants_parts(), "Outfit_Joggers"), voxel=0.003, faces=14000,
                   weight_fn=pants_weight)
    cut(ob, [(V(0, 0, 0.985), V(0, 0, 1)), (V(0, 0, cuff_z), V(0, 0, -1))])
    thicken(ob, 0.004)
    BC.cylinder_uv(ob, 0.80)
    ob.data.materials.append(mat)
    return ob


def build_drawstring(mat):
    g = BC.GeoBatch()
    knot = V(0.0, -0.093, 0.962)
    for sx in (-1, 1):
        loop = [knot, knot + V(sx * 0.018, -0.004, 0.010), knot + V(sx * 0.034, -0.005, 0.004),
                knot + V(sx * 0.028, -0.004, -0.010), knot + V(sx * 0.005, -0.002, -0.002)]
        g.add(*BC.sweep_geo(BC.catmull(loop, 6), 0.0042, 0.0030, ring=6))
        tail = [knot, knot + V(sx * 0.007, -0.003, -0.022), knot + V(sx * 0.009, -0.002, -0.048),
                knot + V(sx * 0.006, 0.0, -0.072)]
        g.add(*BC.sweep_geo(BC.catmull(tail, 6), 0.0034, 0.0027, ring=6))
        g.add(*BC.sweep_geo([knot + V(sx * 0.006, 0, -0.070), knot + V(sx * 0.006, 0, -0.080)], 0.0046, 0.0046, ring=8))
    g.add(*BC.sweep_geo(BC.catmull([knot + V(-0.006, 0, 0), knot + V(0, -0.004, 0), knot + V(0.006, 0, 0)], 4),
                        0.0062, 0.0052, ring=8))
    return g.build("Drawstring", mat)


def join(objs, name):
    for o in bpy.context.view_layer.objects:
        o.select_set(False)
    for o in objs:
        o.select_set(True)
    bpy.context.view_layer.objects.active = objs[0]
    bpy.ops.object.join()
    ob = objs[0]
    ob.name = ob.data.name = name
    return ob


# ---------------------------------------------------------------- main
def main():
    import json
    render = "--no-render" not in sys.argv
    BC.reset()
    face_img = AN.face_texture()
    strand_img = AN.strand_texture()
    plaid = BC.plaid_texture()
    P = AN.PAL
    mats = {
        "skin": AN.pbr("Skin", P["skin"]), "face": AN.pbr("Face", P["skin"], image=face_img),
        "hair": AN.pbr("Hair", "#FFFFFF", image=strand_img, rough=0.5),
        "hair_light": AN.pbr("HairLight", P["hair_light"], rough=0.5),
        "hair_dark": AN.pbr("HairDark", P["hair_dark"], rough=0.5),
        "sweater": AN.pbr("Sweater", P["sweater"], rough=0.95),
        "plaid": AN.pbr("Plaid", P["plaid"], image=plaid, rough=0.95), "string": AN.pbr("Drawstring", P["string"]),
    }
    rig = build_rig()

    print("body")
    body = BT.remesh(BT.union_body())
    body.data.materials.append(mats["skin"])
    bone_heat(body, rig)

    print("head")
    verts, faces = HD.build_mesh()
    head = BC.mesh_obj("Head", verts, faces, mats["face"])
    bm = bmesh.new()
    bm.from_mesh(head.data)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    bm.to_mesh(head.data)
    bm.free()
    AN.face_uvs(head)
    AN.smooth_face_normals(head)
    ears = BC.build_ears_from_drawing(mats["skin"])

    print("hair")
    volume = AN.build_hair_volume(head, mats["hair"])
    clumps = AN.build_hair_clumps(volume, head, mats)
    hair = join([volume] + clumps, "Hair")
    bm = bmesh.new()
    bm.from_mesh(hair.data)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    bm.to_mesh(hair.data)
    bm.free()
    BC.shade_smooth(hair)
    for o in [head, hair] + ears:
        rigid(o, rig)
    hair["wardrobe_slot"] = "hair"
    hair["label"] = "Messy bun"

    print("clothes")
    sweater = build_sweater(mats["sweater"])
    pants = build_pants(mats["plaid"])
    string = build_drawstring(mats["string"])
    pants = join([pants, string], "Outfit_Joggers")
    for ob, slot, label in ((sweater, "top", "Navy sweater"), (pants, "bottom", "Plaid joggers")):
        transfer_weights(body, ob, rig)
        restrict_weights(ob, pants_allowed if slot == "bottom" else sweater_allowed)
        ob["wardrobe_slot"] = slot
        ob["label"] = label
        ob["hides"] = ""

    AN.build_clips(rig)

    objs = [rig, body, head, hair, sweater, pants] + ears
    glb = os.path.join(OUT_EXPORT, "character.glb")
    BC.export_selection(glb, objs, True)
    print("quaternion sign fixes:", AN.fix_quaternion_signs(glb))
    BC.glb_to_embedded_gltf(glb, os.path.join(OUT_EXPORT, "character.gltf"))
    parts = os.path.join(OUT_EXPORT, "parts")
    os.makedirs(parts, exist_ok=True)
    for f in os.listdir(parts):
        os.remove(os.path.join(parts, f))
    BC.export_selection(os.path.join(parts, "base_body.glb"), [rig, body, head] + ears, True)
    AN.fix_quaternion_signs(os.path.join(parts, "base_body.glb"))
    for ob, fn in ((hair, "hair_bun"), (sweater, "sweater"), (pants, "joggers")):
        BC.export_selection(os.path.join(parts, fn + ".glb"), [rig, ob], False)
    json.dump({"skeleton": A.BONE_ORDER,
               "items": [{"node": "Hair", "slot": "hair", "file": "parts/hair_bun.glb"},
                         {"node": "Outfit_Sweater", "slot": "top", "file": "parts/sweater.glb"},
                         {"node": "Outfit_Joggers", "slot": "bottom", "file": "parts/joggers.glb"}]},
              open(os.path.join(OUT_EXPORT, "wardrobe.json"), "w"), indent=2)
    tris = sum(len(p.vertices) - 2 for o in objs if o.type == "MESH" for p in o.data.polygons)
    print(f"EXPORTED {glb} triangles={tris}")
    bpy.context.preferences.filepaths.save_version = 0
    bpy.ops.wm.save_as_mainfile(filepath=os.path.join(OUT_EXPORT, "character.blend"))
    if not render:
        return

    for m in list(bpy.data.materials):
        if m.get("toon_base"):
            key = AN.TOON_SHADE.get(m.name)
            thr = {"Face": 0.16, "Skin": 0.30}.get(m.name, 0.42)
            AN.toonify(m, shade_hex=P[key] if key else None, threshold=thr)
    AN.add_outlines([(head, 0.0016), (hair, 0.0022), (body, 0.0020), (sweater, 0.0024), (pants, 0.0024)] +
                    [(e, 0.0012) for e in ears])
    rig.data.pose_position = "REST"
    cam = AN.setup_eevee()
    stamp = str(int(__import__("time").time()))
    views = [("front", 0), ("threequarter", -32), ("side", -90), ("back", 180), ("back_threequarter", -142)]
    paths = [AN.shoot(cam, az, os.path.join(OUT_RENDER, f"v2_{n}.png")) for n, az in views]
    AN.sheet(paths, os.path.join(OUT_RENDER, "v2_turnaround.png"), ref=os.path.join(ROOT, "reference", "turnaround.png"))
    heads = [AN.shoot(cam, az, os.path.join(OUT_RENDER, f"v2_head_{az}.png"), z=1.54, scale=0.46, res=(600, 600))
             for az in (0, -35, -90, 150)]
    AN.sheet(heads, os.path.join(OUT_RENDER, "v2_heads.png"))
    for o in (sweater, pants, hair):
        o.hide_render = True
    AN.sheet([AN.shoot(cam, az, os.path.join(OUT_RENDER, f"v2_body_{az}.png")) for az in (0, -35, -90)],
             os.path.join(OUT_RENDER, "v2_body.png"))
    for o in (sweater, pants, hair):
        o.hide_render = False
    rig.data.pose_position = "POSE"
    wp = []
    for f in (1, 9, 17, 25):
        BC.set_action(rig, "Walk", f)
        wp.append(AN.shoot(cam, -35, os.path.join(OUT_RENDER, f"v2_walk_{f:02d}.png"), res=(420, 1080)))
    AN.sheet(wp, os.path.join(OUT_RENDER, "v2_walk.png"))
    print("RENDERED", stamp)


if __name__ == "__main__":
    main()
