"""Rig the AI-generated, drawing-painted character and split it into Hair / Skin / Sweater / Joggers.

    python3 character/ai/rig_ai.py -- character/ai/character_ai.glb character/export/character.glb
"""
import math
import os
import sys

import bpy
import bmesh
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
BL = os.path.join(os.path.dirname(HERE), "blender")
sys.path.insert(0, BL)
import head_from_drawing as HD  # noqa: E402
HD.XC = 0.0
import anatomy as A  # noqa: E402
import reference as R  # noqa: E402
import build_character as BC  # noqa: E402
import build_anime as AN  # noqa: E402
import build_v2 as B2  # noqa: E402

args = sys.argv[sys.argv.index("--") + 1:]
src, dst = args[0], args[1]
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.wm.open_mainfile(filepath=src.replace(".glb", ".blend"))
ob = bpy.data.objects["Character"]
for o in list(bpy.data.objects):
    if o is not ob:
        bpy.data.objects.remove(o)

# ---- classify every face by the painted colour under its UV centre
img = ob.data.materials[0].node_tree.nodes["Image Texture"].image if "Image Texture" in ob.data.materials[0].node_tree.nodes \
    else [n.image for n in ob.data.materials[0].node_tree.nodes if n.type == "TEX_IMAGE"][0]
W, H = img.size
px = np.array(img.pixels[:], dtype=np.float32).reshape(H, W, 4)[:, :, :3]
px = np.where(px <= 0.0031308, px * 12.92, 1.055 * np.power(np.clip(px, 0, 1), 1 / 2.4) - 0.055) * 255  # to sRGB
uvl = ob.data.uv_layers.active.data
protos = np.array([p for _, p in R.PROTOS], float)
labs = np.array([c for c, _ in R.PROTOS])
cls = np.zeros(len(ob.data.polygons), int)
cent = np.zeros((len(ob.data.polygons), 3))
for p in ob.data.polygons:
    uv = np.mean([uvl[i].uv[:] for i in p.loop_indices], 0)
    x, y = int(np.clip(uv[0] * W, 0, W - 1)), int(np.clip(uv[1] * H, 0, H - 1))
    c = px[max(0, y - 1):y + 2, max(0, x - 1):x + 2].reshape(-1, 3).mean(0)
    k = labs[((protos - c) ** 2).sum(1).argmin()]
    cls[p.index] = k
    cent[p.index] = p.center[:]
# spatial priors: the drawing's layout decides ambiguous colours
z = cent[:, 2]
cls[(cls == R.LINE)] = R.SKIN
cls[(cls == R.SHIRT) & (z < 0.80)] = R.PANTS
cls[(cls == R.PANTS) & (z > 1.05)] = R.SHIRT
cls[(cls == R.STRING)] = R.PANTS
cls[(cls == R.HAIR) & (z < 1.36)] = R.SKIN          # dark skin shading on hands/feet is not hair
# majority smoothing over face neighbours (3 passes)
bm = bmesh.new()
bm.from_mesh(ob.data)
bm.faces.ensure_lookup_table()
nbrs = [[g.index for e in f.edges for g in e.link_faces if g.index != f.index] for f in bm.faces]
bm.free()
for _ in range(4):
    new = cls.copy()
    for i, nb in enumerate(nbrs):
        if nb:
            vals, cnt = np.unique(cls[nb], return_counts=True)
            if cnt.max() >= 2 and vals[cnt.argmax()] != cls[i]:
                new[i] = vals[cnt.argmax()]
    cls = new
names = {R.HAIR: "Hair", R.SKIN: "Body", R.SHIRT: "Outfit_Sweater", R.PANTS: "Outfit_Joggers"}
print({names.get(k, k): int((cls == k).sum()) for k in np.unique(cls)})

# ---- rig: v2 skeleton scaled to this height, bone heat on the whole mesh
rig = B2.build_rig()
co = np.array([v.co[:] for v in ob.data.vertices])
print("height", co[:, 2].max())
import body_test as BT
proxy = BT.remesh(BT.union_body(), faces=30000)
B2.bone_heat(proxy, rig)
# fit the proxy's height to this mesh
k = co[:, 2].max() / 1.76
proxy.scale = (k, k, k)
bpy.context.view_layer.update()
for o in bpy.context.view_layer.objects:
    o.select_set(False)
B2.transfer_weights(proxy, ob, rig)
bpy.data.objects.remove(proxy)
print("weights transferred from the anatomical proxy")

# pants follow only the legs and pelvis; the sweater never follows hands or legs
vcls = np.full(len(ob.data.vertices), R.SKIN)
for p in ob.data.polygons:
    if cls[p.index] in (R.PANTS, R.SHIRT):
        for vi in p.vertices:
            vcls[vi] = cls[p.index]
ban = {R.PANTS: ("upper_arm", "forearm", "hand", "shoulder", "chest", "neck", "head"),
       R.SHIRT: ("hand", "thigh", "shin", "foot", "toe"),
       "handzone": ("hips", "spine", "thigh", "shin", "foot", "toe", "chest")}
for v in ob.data.vertices:
    if vcls[v.index] == R.SKIN and abs(v.co.x) > 0.165 and 0.70 < v.co.z < 0.99:
        vcls[v.index] = -1
gn = {g.index: g.name for g in ob.vertex_groups}
fixed = 0
for v in ob.data.vertices:
    b = ban.get("handzone" if vcls[v.index] == -1 else vcls[v.index])
    if not b:
        continue
    ws = {gn[g.group]: g.weight for g in v.groups}
    keep = {n: w for n, w in ws.items() if n.split(".")[0] not in b}
    if len(keep) == len(ws):
        continue
    fixed += 1
    if not keep:
        s_ = "L" if v.co.x > 0 else "R"
        keep = {("thigh." + s_) if vcls[v.index] == R.PANTS else ("forearm." + s_) if vcls[v.index] == -1 else "chest": 1.0}
    tot = sum(keep.values())
    for n in ws:
        ob.vertex_groups[n].remove([v.index])
    for n, w in keep.items():
        (ob.vertex_groups.get(n) or ob.vertex_groups.new(name=n)).add([v.index], w / tot, "REPLACE")
print("clothing weight fixes", fixed)
# hand zone: decide arm vs leg by distance to the real forearm/hand bones, not by colour
J = A.joints()
kk = co[:, 2].max() / 1.76
def seg(n):
    h, t = J[n]
    return h * kk, t * kk
for v in ob.data.vertices:
    if not (abs(v.co.x) > 0.15 and 0.66 < v.co.z < 1.00):
        continue
    s_ = "L" if v.co.x > 0 else "R"
    d_arm = min(BC.seg_dist(v.co, *seg(f"forearm.{s_}")), BC.seg_dist(v.co, *seg(f"hand.{s_}")))
    d_leg = BC.seg_dist(v.co, *seg(f"thigh.{s_}"))
    for g in ob.vertex_groups:
        g.remove([v.index])
    if d_arm < 0.042 or d_arm < d_leg * 0.45:
        t = min(1.0, max(0.0, (seg(f"hand.{s_}")[0].z - v.co.z) / 0.03 + 0.5))
        ob.vertex_groups[f"hand.{s_}"].add([v.index], t, "REPLACE")
        ob.vertex_groups[f"forearm.{s_}"].add([v.index], 1 - t + 1e-3, "REPLACE")
    else:
        ob.vertex_groups[f"thigh.{s_}"].add([v.index], 0.6, "REPLACE")
        ob.vertex_groups["hips"].add([v.index], 0.4, "REPLACE")
# neck: blend neck -> head over the jaw, no chest influence
for v in ob.data.vertices:
    if 1.34 < v.co.z <= 1.44 and abs(v.co.x) < 0.12:
        t = (v.co.z - 1.34) / 0.10
        for g in ob.vertex_groups:
            g.remove([v.index])
        ob.vertex_groups["neck"].add([v.index], 1 - t, "REPLACE")
        ob.vertex_groups["head"].add([v.index], t + 1e-3, "REPLACE")
# everything above the chin moves with the head; hair tendrils below keep the neck blend
hv = [v.index for v in ob.data.vertices if v.co.z > 1.44]
for g in ob.vertex_groups:
    g.remove(hv)
ob.vertex_groups["head"].add(hv, 1.0, "REPLACE")
# soften the zone edges
for o in bpy.context.view_layer.objects:
    o.select_set(False)
ob.select_set(True)
bpy.context.view_layer.objects.active = ob
bpy.ops.object.mode_set(mode="WEIGHT_PAINT")
bpy.ops.object.vertex_group_smooth(group_select_mode="ALL", factor=0.5, repeat=3, expand=0.0)
bpy.ops.object.vertex_group_limit_total(group_select_mode="ALL", limit=4)
bpy.ops.object.vertex_group_normalize_all(lock_active=False)
bpy.ops.object.vertex_group_limit_total(group_select_mode="ALL", limit=4)
bpy.ops.object.vertex_group_normalize_all(lock_active=False)
bpy.ops.object.mode_set(mode="OBJECT")
# ---- split into parts (weights and UVs carry over)
mats = {}
for k, n in names.items():
    m = ob.data.materials[0].copy()
    m.name = n
    mats[k] = m
ob.data.materials.clear()
for k in names:
    ob.data.materials.append(mats[k])
order = list(names)
for p in ob.data.polygons:
    p.material_index = order.index(cls[p.index]) if cls[p.index] in order else order.index(R.SKIN)
for o in bpy.context.view_layer.objects:
    o.select_set(False)
ob.select_set(True)
bpy.context.view_layer.objects.active = ob
bpy.ops.object.mode_set(mode="EDIT")
bpy.ops.mesh.separate(type="MATERIAL")
bpy.ops.object.mode_set(mode="OBJECT")
parts = [o for o in bpy.data.objects if o.type == "MESH"]
for o in parts:
    used = {p.material_index for p in o.data.polygons}
    mname = o.data.materials[list(used)[0]].name if used else "Body"
    o.name = o.data.name = mname
    slot = {"Hair": "hair", "Outfit_Sweater": "top", "Outfit_Joggers": "bottom"}.get(mname, "body")
    o["wardrobe_slot"] = slot
    o["label"] = {"Hair": "Messy bun", "Outfit_Sweater": "Navy sweater", "Outfit_Joggers": "Plaid joggers"}.get(mname, "")
    print("part", mname, len(o.data.polygons), "faces")

AN.ARM_SWING = 0.12          # relaxed pajama walk; the generated mesh has hands resting on the hips
AN.build_clips(rig)
objs = [rig] + parts
BC.export_selection(dst, objs, True)
print("quaternion fixes", AN.fix_quaternion_signs(dst))
BC.glb_to_embedded_gltf(dst, dst.replace(".glb", ".gltf"))
bpy.context.preferences.filepaths.save_version = 0
bpy.ops.wm.save_as_mainfile(filepath=dst.replace(".glb", ".blend"))

# ---- renders: painted colours, rest pose and walk
for o in parts:
    for m in o.data.materials:
        b = m.node_tree.nodes.get("Principled BSDF")
        t = [n for n in m.node_tree.nodes if n.type == "TEX_IMAGE"][0]
        m.node_tree.links.new(t.outputs["Color"], b.inputs["Emission Color"])
        b.inputs["Emission Strength"].default_value = 0.75
sc = bpy.context.scene
sc.render.engine = "CYCLES"
sc.cycles.samples = 24
sc.cycles.use_denoising = True
sc.render.film_transparent = True
w = bpy.data.worlds.new("w")
sc.world = w
w.use_nodes = True
w.node_tree.nodes["Background"].inputs[1].default_value = 1.0
L = bpy.data.lights.new("k", "SUN")
L.energy = 2.5
lo = bpy.data.objects.new("k", L)
sc.collection.objects.link(lo)
lo.rotation_euler = (0.9, 0, -0.6)
cam = bpy.data.cameras.new("c")
cam.type = "ORTHO"
cam.ortho_scale = 1.9
co_ = bpy.data.objects.new("c", cam)
sc.collection.objects.link(co_)
sc.camera = co_
from PIL import Image
out_dir = os.path.join(os.path.dirname(HERE), "renders")


def shoot(az, path, res=(420, 900), z=0.88, scale=1.9):
    sc.render.resolution_x, sc.render.resolution_y = res
    cam.ortho_scale = scale
    a = math.radians(az)
    co_.location = (math.sin(a) * 5, -math.cos(a) * 5, z)
    co_.rotation_euler = (math.pi / 2, 0, a)
    sc.render.filepath = path
    bpy.ops.render.render(write_still=True)
    return Image.open(path).convert("RGBA")


def sheet(ims, out):
    s = Image.new("RGBA", (sum(i.width for i in ims), ims[0].height), "white")
    x = 0
    for i in ims:
        s.alpha_composite(i, (x, 0))
        x += i.width
    s.convert("RGB").save(out)


rig.data.pose_position = "REST"
sheet([shoot(az, f"/tmp/ai_{az}.png") for az in (0, -32, -90, 180, -142)], os.path.join(out_dir, "ai_turnaround.png"))
sheet([shoot(az, f"/tmp/aih_{az}.png", res=(500, 500), z=1.55, scale=0.45) for az in (0, -35, -90, 150)],
      os.path.join(out_dir, "ai_heads.png"))
rig.data.pose_position = "POSE"
ims = []
for f in (1, 9, 17, 25):
    BC.set_action(rig, "Walk", f)
    ims.append(shoot(-35, f"/tmp/aiw_{f}.png"))
sheet(ims, os.path.join(out_dir, "ai_walk.png"))
print("DONE")
