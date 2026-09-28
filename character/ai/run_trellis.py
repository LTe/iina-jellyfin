import shutil, sys
from gradio_client import Client, handle_file
c = Client('trellis-community/TRELLIS')
c.predict(api_name="/start_session")
multi = sys.argv[1] == 'multi'
imgs = ['in_front.png', 'in_threequarter.png', 'in_side.png', 'in_back.png']
kw = dict(image=handle_file('in_front.png'), seed=7, ss_guidance_strength=7.5, ss_sampling_steps=12,
          slat_guidance_strength=3.0, slat_sampling_steps=12, mesh_simplify=0.9, texture_size=2048,
          api_name="/generate_and_extract_glb")
if multi:
    kw['multiimages'] = [{"image": handle_file(p), "caption": None} for p in imgs]
    kw['multiimage_algo'] = 'multidiffusion'
r = c.predict(**kw)
print(r)
for f in r:
    if isinstance(f, dict): f = f.get('value') or f.get('video')
    if isinstance(f, str) and f.endswith('.glb'):
        shutil.copy(f, f'trellis_{sys.argv[1]}.glb'); break
