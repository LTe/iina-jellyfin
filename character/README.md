# Character model: "Pajama Girl"

A rigged, game-ready 3D model of the character in `reference/turnaround.png`, generated procedurally with Blender's Python API.

## Build

```sh
pip install bpy pillow numpy        # Blender 5.0 as a Python module
python3 character/blender/build_character.py            # build, export, render
python3 character/blender/build_character.py --no-render
```

## Outputs

| Path | What |
| --- | --- |
| `export/character.glb` | Game asset: meshes, skin (21 bones), textures, `Idle` and `Walk` clips. Meters, Y-up, facing +Z |
| `export/character.gltf` | The same asset as a single embedded-JSON glTF |
| `export/character.blend` | Blender scene |
| `renders/` | Cycles turnaround renders, a comparison against the reference, face close-ups |
| `textures/` | Generated face and plaid textures |
| `viewer/index.html` | three.js viewer (serve `character/` over HTTP and open `viewer/`) |

All shapes, colors and proportions are parameters at the top of each `build_*` function, so iterating on the model means editing numbers and re-running the script.
