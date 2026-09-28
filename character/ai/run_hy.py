import sys, shutil, json
from gradio_client import Client, handle_file
c = Client(sys.argv[3] if len(sys.argv) > 3 else 'tencent/Hunyuan3D-2')
mode = sys.argv[1]
kw = dict(steps=int(__import__('os').environ.get('STEPS', 50)), guidance_scale=5.0, seed=1234, octree_resolution=380, check_box_rembg=True,
          num_chunks=8000, randomize_seed=False, api_name=sys.argv[2] if len(sys.argv) > 2 else "/generation_all")
if mode == 'mv':
    kw.update(mv_image_front=handle_file('in_front.png'), mv_image_back=handle_file('in_back.png'),
              mv_image_right=handle_file('in_side.png'), mv_image_left=handle_file('in_side_mirror.png'))
if mode == 'mv2':
    kw.update(mv_image_front=handle_file('in_front.png'), mv_image_back=handle_file('in_back.png'),
              mv_image_left=handle_file('in_side.png'), mv_image_right=handle_file('in_side_mirror.png'))
else:
    kw.update(image=handle_file('in_front.png'))
r = c.predict(**kw)
print(r)
n = 0
for f in r:
    if isinstance(f, dict):
        f = f.get("value") or f.get("path")
    if isinstance(f, str) and f.endswith((".glb", ".obj", ".ply")):
        shutil.copy(f, f"hy_{mode}_{sys.argv[2].strip('/')}_{n}.glb"); n += 1
