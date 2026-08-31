# In-chat image editing (Qwen Image Edit) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Open WebUI's "edit image" feature work end to end against
Qwen Image Edit running on ComfyUI, with the same offline-validation and
live-check discipline the existing in-chat generation feature has.

**Architecture:** Reuse the generation feature's push-to-database mechanism
(`scripts/owui_image_config.py` → Open WebUI's admin API → `ImagesConfig`
database rows) on the parallel `images.edit.*` config subtree. Add a second,
independent workflow family (`inventory/comfyui-edit-workflows/`) rather than
folding editing into the existing `inventory/comfyui-workflows/` directory,
because the two mappings target structurally different graphs and the
existing cross-workflow consistency check would otherwise check one family's
node IDs against the other's.

**Tech Stack:** Ansible (svc-infra deploy surface, untouched except one static
token file and two new systemd units), Python 3 stdlib scripts (no new
dependencies — `owui_image_config.py` already depends on `pyyaml`, already a
requirement), ComfyUI running natively on the Windows GPU host (this machine,
TERRA — confirmed reachable at `192.168.1.40:8188` and locally at
`localhost:8188`/`localhost:11434` while this plan is executed on it).

**Spec:**
[docs/superpowers/specs/2026-08-27-comfyui-image-editing-design.md](../specs/2026-08-27-comfyui-image-editing-design.md)
("the design"), which itself builds on
[docs/superpowers/specs/2026-08-14-comfyui-image-generation-design.md](../specs/2026-08-14-comfyui-image-generation-design.md)
("the generation design").

## Global Constraints

- **Never echo vault secrets** to the terminal, logs, or a commit;
  `OWUI_ADMIN_TOKEN` is read from the environment or `--token-file`, never
  from `vault.yml` inside a `make` recipe (generation design, "Secret
  handling").
- **Never commit `vault.yml`.** Gitignored, stays that way.
- **Never `git add -A`.** Stage explicit paths.
- **No play or role may read `images.yml` or the workflow files** — grep
  `roles/` and the top-level `*.yml` plays to confirm this stays true after
  every task that touches the catalog (design, §6; generation design,
  Architecture table).
- **`comfyui_base_url`/`image_edit_enabled`-style catalog fields must agree
  with `gpu_host_ip`/`gpu_host_online` in `main.yml`** — the validator fails
  the build on drift (existing rule, extended to the edit keys in Task 4).
- **Verify by using the feature, not by checking a green container** — a
  `make image-edit-check` run returning `ok` is what "works" means here
  (CLAUDE.md, "Verification means the application works").
- **`make infra` must report `changed=0` against a clean tree** (or, on the
  first deploy after a commit, the three-task `svc-infra` sync exception,
  settling to `changed=0` on a second run) before this branch merges
  (CLAUDE.md, "The change workflow").
- **Follow the CLAUDE.md change workflow**: branch, edit, `make validate`,
  deploy iteratively while developing, commit only once finished, confirm a
  clean tree, final deploy + `make verify`, merge, delete the branch.

---

## File Structure

| Path | Status | Responsibility |
|---|---|---|
| `inventory/comfyui-edit-workflows/qwen-image-edit.json` | Create | The API-format edit workflow. A new sibling directory to `inventory/comfyui-workflows/`, not a member of it — design §4. |
| `inventory/group_vars/all/images.yml` | Modify | Add the `image_edit_*` catalog keys, mirroring the existing `image_*` generation keys. |
| `tests/validate_openwebui_image_config.py` | Modify | Add a second `check_config`-shaped pass for the edit family: its own `CLASS_INPUTS` entries, its own required/forbidden node types, its own cross-workflow-within-family check. |
| `scripts/owui_image_config.py` | Modify | `managed_keys()` gains the edit keys; `SHOWABLE` gains the edit scalars safe to print in a diff. |
| `scripts/image_edit_check.py` | Create | The runtime, end-to-end proof — mirrors `scripts/image_generation_check.py`'s shape, with a different positive-proof strategy (design §7). |
| `Makefile` | Modify | New `image-edit-check` target; `.PHONY` line gains it. |
| `roles/svc_infra/files/homelab-image-edit@.service` | Create | Nightly check unit, mirrors `homelab-image-gen@.service`. |
| `roles/svc_infra/files/homelab-image-edit@.timer` | Create | Nightly check timer, staggered after `homelab-image-gen@.timer`. |
| `roles/svc_infra/tasks/verify-runner.yml` | Modify | Install and arm the two new units, mirroring the existing image-gen block. |
| `docs/gpu-host.md` | Modify | Record the three downloaded files, their hashes, and the measured VRAM figures — the same table shape already used for Pony/SDXL/Flux/Z-Image/H3. |
| `docs/plans/image-editing.md` | Modify | Flip its "not implemented" status to point at the design and this plan. |

No file under `roles/` other than the two new unit installs changes the
deploy surface, and those two are static files that settle to `changed=0`
after their first deploy — the same shape as the existing image-gen-check
token/unit deployment.

---

### Task 1: Source and verify the Qwen Image Edit checkpoint files

**Files:** none in the repo. Downloads land in
`C:\ComfyUI\ComfyUI_windows_portable\ComfyUI\models\{diffusion_models,text_encoders,vae}\`
on this machine (TERRA is the GPU host — confirmed: `hostname` returns
`terra`, `C:\ComfyUI\ComfyUI_windows_portable\` exists, and
`http://192.168.1.40:8188/system_stats` answers locally).

**Interfaces:**
- Produces: three verified `.safetensors` files at the paths above, whose
  exact filenames Task 2 references when building the workflow and Task 4
  writes into `image_edit_model` / `image_edit_workflow_nodes`.

- [ ] **Step 1: Confirm free disk space**

```bash
df -h /c
```

Expected: at least 35 GB free (the three files total ~27.7 GiB; leave
headroom). If short, stop and free space before downloading — do not resume
a `.part` file onto a full disk.

- [ ] **Step 2: Download the diffusion model, resumable, to a `.part` file**

```bash
curl.exe -L --fail --no-progress-meter -C - \
  -o "C:\ComfyUI\ComfyUI_windows_portable\ComfyUI\models\diffusion_models\qwen_image_edit_fp8_e4m3fn.safetensors.part" \
  "https://huggingface.co/Comfy-Org/Qwen-Image-Edit_ComfyUI/resolve/main/split_files/diffusion_models/qwen_image_edit_fp8_e4m3fn.safetensors"
```

Do not pipe through `2>&1` (native stderr becomes ErrorRecords under
`$ErrorActionPreference='Stop'` and kills progress output — the same trap
`docs/gpu-host.md` already documents for `curl.exe`).

- [ ] **Step 3: Verify its hash before renaming into place**

```bash
sha256sum "C:\ComfyUI\ComfyUI_windows_portable\ComfyUI\models\diffusion_models\qwen_image_edit_fp8_e4m3fn.safetensors.part"
```

Expected: `393c6743d1de2e9031b5197027b36116f2096958ccc0223526d34e1860266021`.
If it disagrees, delete the `.part` and redownload — do not rename a
truncated file into place.

```bash
mv "C:\ComfyUI\ComfyUI_windows_portable\ComfyUI\models\diffusion_models\qwen_image_edit_fp8_e4m3fn.safetensors.part" \
   "C:\ComfyUI\ComfyUI_windows_portable\ComfyUI\models\diffusion_models\qwen_image_edit_fp8_e4m3fn.safetensors"
```

- [ ] **Step 4: Repeat for the text encoder**

```bash
curl.exe -L --fail --no-progress-meter -C - \
  -o "C:\ComfyUI\ComfyUI_windows_portable\ComfyUI\models\text_encoders\qwen_2.5_vl_7b_fp8_scaled.safetensors.part" \
  "https://huggingface.co/Comfy-Org/Qwen-Image_ComfyUI/resolve/main/split_files/text_encoders/qwen_2.5_vl_7b_fp8_scaled.safetensors"
sha256sum "C:\ComfyUI\ComfyUI_windows_portable\ComfyUI\models\text_encoders\qwen_2.5_vl_7b_fp8_scaled.safetensors.part"
```

Expected hash: `cb5636d852a0ea6a9075ab1bef496c0db7aef13c02350571e388aea959c5c0b4`.
Then rename off `.part` the same way as Step 3.

- [ ] **Step 5: Repeat for the VAE**

```bash
curl.exe -L --fail --no-progress-meter -C - \
  -o "C:\ComfyUI\ComfyUI_windows_portable\ComfyUI\models\vae\qwen_image_vae.safetensors.part" \
  "https://huggingface.co/Comfy-Org/Qwen-Image_ComfyUI/resolve/main/split_files/vae/qwen_image_vae.safetensors"
sha256sum "C:\ComfyUI\ComfyUI_windows_portable\ComfyUI\models\vae\qwen_image_vae.safetensors.part"
```

Expected hash: `a70580f0213e67967ee9c95f05bb400e8fb08307e017a924bf3441223e023d1f`.
Then rename off `.part`.

- [ ] **Step 6: Restart ComfyUI and confirm all three are visible live**

Stop whatever is running on port 8188 and relaunch
`C:\ComfyUI\start-comfyui-lan.bat`, then:

```bash
curl -s http://192.168.1.40:8188/object_info/UNETLoader | python3 -c "
import json,sys
print('qwen_image_edit_fp8_e4m3fn.safetensors' in json.load(sys.stdin)['UNETLoader']['input']['required']['unet_name'][0])
"
curl -s http://192.168.1.40:8188/object_info/CLIPLoader | python3 -c "
import json,sys
print('qwen_2.5_vl_7b_fp8_scaled.safetensors' in json.load(sys.stdin)['CLIPLoader']['input']['required']['clip_name'][0])
"
curl -s http://192.168.1.40:8188/object_info/VAELoader | python3 -c "
import json,sys
print('qwen_image_vae.safetensors' in json.load(sys.stdin)['VAELoader']['input']['required']['vae_name'][0])
"
```

Expected: `True` from all three. This is the step that proves the files are
not just present on disk but actually loadable ComfyUI options — a
misspelled destination filename would fail silently otherwise (the same
class of failure `docs/gpu-host.md` records for the H3 install).

No commit for this task — nothing here is repo-tracked.

---

### Task 2: Confirm live node input names, then hand-build and prove the API-format workflow

**Files:**
- Create: `inventory/comfyui-edit-workflows/qwen-image-edit.json`

**Interfaces:**
- Consumes: the three filenames verified loadable in Task 1.
- Produces: a committed, API-format workflow file whose node IDs and
  `class_type`s Task 4 (validator) and Task 5 (catalog mapping) reference by
  exact string.

- [ ] **Step 1: Query `/object_info` for every class this workflow introduces**

The design (§4) lists the input names read from the template's own JSON as
provisional. Confirm each against the live host before trusting it:

```bash
for cls in TextEncodeQwenImageEdit CFGNorm ModelSamplingAuraFlow ImageScaleToTotalPixels LoadImage; do
  echo "=== $cls ==="
  curl -s "http://192.168.1.40:8188/object_info/$cls" | python3 -c "
import json,sys
d = json.load(sys.stdin)
info = next(iter(d.values()))
print('required:', list(info['input'].get('required', {}).keys()))
print('optional:', list(info['input'].get('optional', {}).keys()))
"
done
```

Record the output. If any input name differs from the design's table
(`clip, vae, image` for `TextEncodeQwenImageEdit`'s LoadImage-fed inputs plus
a `prompt` widget; `model` for `CFGNorm` and `ModelSamplingAuraFlow`; `image`
for `LoadImage`), use the live names in every step below instead of the
design's — the design says explicitly that the template JSON is provisional
pending this check.

- [ ] **Step 2: Create the workflow directory and write the flat API-format graph**

```bash
mkdir -p inventory/comfyui-edit-workflows
```

Write `inventory/comfyui-edit-workflows/qwen-image-edit.json` using the
node IDs below (chosen to avoid colliding with `sdxl.json`/`pony.json`'s IDs
purely for human readability when diffing both directories side by side —
collision would not actually matter, since Task 4's validator checks each
family only against its own directory):

```json
{
  "10": {"class_type": "UNETLoader",
         "inputs": {"unet_name": "qwen_image_edit_fp8_e4m3fn.safetensors", "weight_dtype": "default"}},
  "11": {"class_type": "CLIPLoader",
         "inputs": {"clip_name": "qwen_2.5_vl_7b_fp8_scaled.safetensors", "type": "qwen_image", "device": "default"}},
  "12": {"class_type": "VAELoader",
         "inputs": {"vae_name": "qwen_image_vae.safetensors"}},
  "13": {"class_type": "LoadImage",
         "inputs": {"image": "example.png"}},
  "14": {"class_type": "ImageScaleToTotalPixels",
         "inputs": {"image": ["13", 0], "upscale_method": "lanczos", "megapixels": 1.0}},
  "15": {"class_type": "TextEncodeQwenImageEdit",
         "inputs": {"clip": ["11", 0], "vae": ["12", 0], "image": ["14", 0], "prompt": "a photo"}},
  "16": {"class_type": "TextEncodeQwenImageEdit",
         "inputs": {"clip": ["11", 0], "vae": ["12", 0], "image": ["14", 0], "prompt": " "}},
  "17": {"class_type": "ModelSamplingAuraFlow",
         "inputs": {"model": ["10", 0], "shift": 3}},
  "18": {"class_type": "CFGNorm",
         "inputs": {"model": ["17", 0], "strength": 1}},
  "19": {"class_type": "VAEEncode",
         "inputs": {"pixels": ["14", 0], "vae": ["12", 0]}},
  "20": {"class_type": "KSampler",
         "inputs": {"model": ["18", 0], "positive": ["15", 0], "negative": ["16", 0],
                     "latent_image": ["19", 0], "seed": 0, "steps": 50, "cfg": 4.0,
                     "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0}},
  "21": {"class_type": "VAEDecode",
         "inputs": {"samples": ["20", 0], "vae": ["12", 0]}},
  "22": {"class_type": "SaveImage",
         "inputs": {"filename_prefix": "homelab-owui-edit", "images": ["21", 0]}}
}
```

`node "13"`'s `image` field (`"example.png"`) is a placeholder ComfyUI
filename — the mapping in Task 5 overwrites it with the real uploaded
filename on every request, exactly like `ckpt_name` in `sdxl.json`/`pony.json`
today. If Step 1 found different input names than assumed here, edit the
`inputs` keys accordingly before continuing — do not proceed with names that
disagree with the live `/object_info` output.

- [ ] **Step 3: Prove the graph executes directly against ComfyUI, bypassing Open WebUI**

Before this workflow is wired into any Open WebUI config, prove it runs at
all. Upload a tiny test image and submit the graph straight to `/prompt`:

```bash
python3 - <<'PYEOF'
import json, struct, urllib.request, uuid, zlib

def tiny_png(width=64, height=64, rgb=(120, 90, 200)):
    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data +
                struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff))
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    idat = chunk(b"IDAT", zlib.compress(raw, 9))
    return sig + ihdr + idat + chunk(b"IEND", b"")

base = "http://192.168.1.40:8188"

boundary = uuid.uuid4().hex
body = (
    f"--{boundary}\r\n"
    'Content-Disposition: form-data; name="image"; filename="probe.png"\r\n'
    "Content-Type: image/png\r\n\r\n"
).encode() + tiny_png() + (
    f"\r\n--{boundary}\r\n"
    'Content-Disposition: form-data; name="type"\r\n\r\ninput\r\n'
    f"--{boundary}--\r\n"
).encode()
req = urllib.request.Request(f"{base}/api/upload/image", data=body,
    headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
uploaded = json.loads(urllib.request.urlopen(req, timeout=30).read())
print("uploaded as:", uploaded["name"])

workflow = json.loads(open("inventory/comfyui-edit-workflows/qwen-image-edit.json").read())
workflow["13"]["inputs"]["image"] = uploaded["name"]
workflow["15"]["inputs"]["prompt"] = "make the background bright red"

req = urllib.request.Request(f"{base}/prompt",
    data=json.dumps({"prompt": workflow, "client_id": uuid.uuid4().hex}).encode(),
    headers={"Content-Type": "application/json"})
res = json.loads(urllib.request.urlopen(req, timeout=30).read())
print("queued:", res)
PYEOF
```

- [ ] **Step 4: Poll `/history` for completion, confirm a SaveImage output, and read VRAM**

```bash
PROMPT_ID=<the prompt_id printed above>
for i in $(seq 1 60); do
  RESULT=$(curl -s "http://192.168.1.40:8188/history/$PROMPT_ID")
  if [ "$RESULT" != "{}" ]; then break; fi
  sleep 5
done
echo "$RESULT" | python3 -m json.tool | head -40
nvidia-smi --query-gpu=memory.used,memory.total --format=csv
```

Expected: the history entry's `outputs."22"` (the `SaveImage` node) lists a
produced image; `nvidia-smi` reports the peak this run used (read it during
generation if possible, since ComfyUI's caching allocator does not release
back to idle immediately after — the same "measure a clean order" trap
`docs/gpu-host.md` records for Flux/Z-Image/SDXL). Record this number.

- [ ] **Step 5: Restart ComfyUI once more for a clean idle baseline, then measure the real peak**

```bash
# stop the running ComfyUI process, relaunch start-comfyui-lan.bat
nvidia-smi --query-gpu=memory.used --format=csv   # idle baseline, record it
```

Re-run Step 3's submission (fresh `client_id`, different seed so the cache
does not short-circuit — `docs/gpu-host.md`'s "do not verify by re-running an
identical workflow" rule), and poll `nvidia-smi` a few times during
execution rather than only after. Record: idle baseline, peak used, headroom
against 24564 MiB. **If it OOMs**, this is the moment to know it — stop and
report back rather than continuing to Task 4 wiring a checkpoint that does
not fit; a smaller tier (`qwen_image_edit_2509_fp8_e4m3fn.safetensors`,
same size — no smaller tier exists below fp8 without a bf16-vs-int8
retraining tradeoff) is not currently available, so an OOM here blocks the
feature and needs a decision, not a workaround.

- [ ] **Step 6: Commit the workflow file**

```bash
git add inventory/comfyui-edit-workflows/qwen-image-edit.json
git commit -m "feat: add Qwen Image Edit ComfyUI workflow, proven directly against ComfyUI"
```

---

### Task 3: Add the edit catalog keys to `images.yml`

**Files:**
- Modify: `inventory/group_vars/all/images.yml`

**Interfaces:**
- Consumes: `qwen_image_edit_fp8_e4m3fn.safetensors` (Task 1),
  `inventory/comfyui-edit-workflows/qwen-image-edit.json` (Task 2), node IDs
  `"10"`–`"22"` from that file.
- Produces: `image_edit_enabled`, `image_edit_workflow`,
  `image_edit_model`, `image_edit_workflow_nodes` — the keys Task 4's
  validator and Task 6's push tool read by these exact names.

- [ ] **Step 1: Append the edit keys**

Add to `inventory/group_vars/all/images.yml`, after the existing generation
keys:

```yaml
# --- Editing (IMAGES_EDIT_COMFYUI_*, a separate ImagesConfig subtree from
# generation's COMFYUI_* above) ---
#
# image_edit_enabled DUPLICATES gpu_host_online, same rule and same reason as
# image_generation_enabled above.
image_edit_enabled: true

# Which file in inventory/comfyui-edit-workflows/ (NOT comfyui-workflows/ —
# the edit family is a separate directory because its mapping targets a
# structurally different graph; see
# docs/superpowers/specs/2026-08-27-comfyui-image-editing-design.md §4).
image_edit_workflow: qwen-image-edit

# The UNETLoader filename, as ComfyUI lists it in
# /object_info/UNETLoader. Verified present on the GPU host in Task 1.
image_edit_model: "qwen_image_edit_fp8_e4m3fn.safetensors"

# No image_edit_size: leaving IMAGE_EDIT_SIZE unset means Open WebUI never
# forwards width/height to ComfyUI, which is correct here — the workflow
# derives output size from the input image via ImageScaleToTotalPixels, not
# from a fixed target (design §1).
#
# No negative_prompt entry below: ComfyUIEditImageForm has no such field, so
# a negative_prompt-type mapping entry would raise AttributeError inside
# _apply_workflow_nodes and be swallowed into a silent None (design §1). The
# fixed negative prompt (" ", Qwen's own documented default) lives baked into
# node "16" of qwen-image-edit.json instead.
image_edit_workflow_nodes:
  - type: model
    key: unet_name
    node_ids: ["10"]
  - type: prompt
    key: prompt
    node_ids: ["15"]
  - type: image
    key: image
    node_ids: ["13"]
  - type: steps
    key: steps
    node_ids: ["20"]
  - type: seed
    key: seed
    node_ids: ["20"]
```

- [ ] **Step 2: Confirm nothing reads this file that would break the deploy guarantee**

```bash
grep -rn "image_edit_" roles/ *.yml
```

Expected: no output (mirrors the existing comment at the top of the same
file — "NO PLAY OR ROLE CONSUMES THESE VARIABLES").

- [ ] **Step 3: Commit**

```bash
git add inventory/group_vars/all/images.yml
git commit -m "feat: add Qwen Image Edit keys to the images.yml catalog"
```

(`make validate` will fail at this point — the validator does not know these
keys yet. That is expected; Task 4 makes it pass.)

---

### Task 4: Extend the offline validator for the edit family

**Files:**
- Modify: `tests/validate_openwebui_image_config.py`
- Test: the file's own `VALIDATION_CASES` self-check (no separate test file
  — this repo's convention, per the existing `validation_self_check()`).

**Interfaces:**
- Consumes: `image_edit_workflow`, `image_edit_model`,
  `image_edit_workflow_nodes` from `images.yml` (Task 3); the workflow at
  `inventory/comfyui-edit-workflows/<image_edit_workflow>.json` (Task 2).
- Produces: `main()` exits 0 only if both the generation and edit mappings
  are structurally sound — the gate later tasks (and the nightly gate) rely
  on before anything gets pushed live.

- [ ] **Step 1: Add the edit-family constants and fixtures near the top of the file**

```python
WORKFLOW_DIR = ROOT / "inventory" / "comfyui-workflows"
EDIT_WORKFLOW_DIR = ROOT / "inventory" / "comfyui-edit-workflows"

# Mapping entries an edit config cannot work without. Unlike generation,
# width/height are not required (design §1 — ComfyUIEditImageForm forwards
# them only if IMAGE_EDIT_SIZE is set, and this workflow derives size from
# the input image instead).
EDIT_REQUIRED_TYPES = {"model", "prompt", "image", "steps", "seed"}

# negative_prompt has no field on ComfyUIEditImageForm at all (design §1) —
# unlike generation, where it is merely optional-and-safe, here it is
# actively wrong: _apply_workflow_nodes would raise AttributeError on
# payload.negative_prompt, swallowed into a silent None by comfyui_edit_image.
EDIT_FORBIDDEN_TYPES = {"negative_prompt"}
```

Extend the existing `CLASS_INPUTS` dict (do not create a second one — both
families' mappings are checked against the same table, since a class like
`KSampler` is legitimately shared):

```python
CLASS_INPUTS.update({
    "UNETLoader": {"unet_name", "weight_dtype"},
    "CLIPLoader": {"clip_name", "type", "device"},
    "VAELoader": {"vae_name"},
    "LoadImage": {"image"},
    "TextEncodeQwenImageEdit": {"clip", "vae", "image", "prompt"},
    "ModelSamplingAuraFlow": {"model", "shift"},
    "CFGNorm": {"model", "strength"},
    "ImageScaleToTotalPixels": {"image", "upscale_method", "megapixels"},
    "VAEEncode": {"pixels", "vae"},
})
```

**If Task 2's Step 1 found different input names than the ones above**, use
those instead — this table must match the live `/object_info` output, not
guessed names.

- [ ] **Step 2: Add `good_edit_catalog()` and `good_edit_workflow()` fixtures**

```python
def good_edit_catalog() -> dict:
    return {
        "image_edit_workflow": "qwen-image-edit",
        "image_edit_enabled": True,
        "image_edit_model": "qwen_image_edit_fp8_e4m3fn.safetensors",
        "image_edit_workflow_nodes": [
            {"type": "model", "key": "unet_name", "node_ids": ["10"]},
            {"type": "prompt", "key": "prompt", "node_ids": ["15"]},
            {"type": "image", "key": "image", "node_ids": ["13"]},
            {"type": "steps", "key": "steps", "node_ids": ["20"]},
            {"type": "seed", "key": "seed", "node_ids": ["20"]},
        ],
    }


def good_edit_workflow() -> dict:
    return {
        "10": {"class_type": "UNETLoader",
               "inputs": {"unet_name": "qwen_image_edit_fp8_e4m3fn.safetensors"}},
        "13": {"class_type": "LoadImage", "inputs": {"image": "example.png"}},
        "15": {"class_type": "TextEncodeQwenImageEdit",
               "inputs": {"clip": None, "vae": None, "image": None, "prompt": ""}},
        "20": {"class_type": "KSampler",
               "inputs": {"seed": 0, "steps": 50, "cfg": 4.0}},
        "22": {"class_type": "SaveImage",
               "inputs": {"filename_prefix": "x", "images": None}},
    }
```

- [ ] **Step 3: Write `check_edit_config()`, the edit-family analogue of `check_config()`**

Add this function. It deliberately duplicates `check_config()`'s shape
rather than parameterizing over both, because the two families' rule sets
differ (`EDIT_REQUIRED_TYPES` vs `REQUIRED_TYPES`, `EDIT_FORBIDDEN_TYPES` vs
the hardcoded `image`-forbidden rule) and a shared function threading both
rule sets through every branch would be harder to read than two similar
functions — this repo already accepts that tradeoff for `good_catalog()` vs
`good_edit_catalog()`.

```python
def check_edit_config(catalog: dict, edit_workflows: dict[str, dict]) -> list[str]:
    """Return human-readable failures for the EDIT mapping. Empty means sound."""
    failures: list[str] = []

    selected = catalog.get("image_edit_workflow")
    if selected not in edit_workflows:
        failures.append(
            f"image_edit_workflow is {selected!r} but no such file exists in "
            f"inventory/comfyui-edit-workflows/ (have: {sorted(edit_workflows)})"
        )

    for name, workflow in sorted(edit_workflows.items()):
        if not isinstance(workflow, dict):
            failures.append(f"edit workflow {name!r} is not a JSON object")
            continue
        if isinstance(workflow.get("nodes"), list):
            failures.append(
                f"edit workflow {name!r} looks like ComfyUI's editor format. "
                "Open WebUI can only read the API format — re-export with "
                "Workflow -> Export (API), or hand-flatten as this repo does"
            )
            continue
        bad = [nid for nid, node in workflow.items()
               if not isinstance(node, dict) or "class_type" not in node
               or "inputs" not in node]
        if bad:
            failures.append(
                f"edit workflow {name!r} nodes {sorted(bad)} lack "
                "class_type/inputs — not valid API format"
            )
            continue
        classes = {node["class_type"] for node in workflow.values()}
        if not (classes & OUTPUT_CLASSES):
            failures.append(
                f"edit workflow {name!r} contains no SaveImage or "
                "PreviewImage node — _ws_get_images would return no images"
            )

    nodes = catalog.get("image_edit_workflow_nodes") or []
    if not isinstance(nodes, list):
        failures.append("image_edit_workflow_nodes must be a list")
        return failures

    seen_types: set[str] = set()
    for index, node in enumerate(nodes):
        node_type = node.get("type")
        where = f"image_edit_workflow_nodes[{index}] (type={node_type!r})"
        seen_types.add(node_type)

        if node_type is not None and node_type not in HANDLED_TYPES:
            failures.append(
                f"{where} is not a type Open WebUI handles"
            )
        if node_type in EDIT_FORBIDDEN_TYPES:
            failures.append(
                f"{where} has no field on ComfyUIEditImageForm. "
                "_apply_workflow_nodes would raise AttributeError, swallowed "
                "by comfyui_edit_image into a silent None — no image, no error"
            )
        if node_type in NEEDS_EXPLICIT_KEY and "key" not in node:
            failures.append(f"{where} needs an explicit key")

        node_ids = node.get("node_ids")
        if (not isinstance(node_ids, list) or not node_ids
                or not all(isinstance(nid, str) for nid in node_ids)):
            failures.append(
                f"{where} has node_ids {node_ids!r}; it must be a non-empty "
                "list of strings"
            )
            continue

        for node_id in node_ids:
            for name, workflow in sorted(edit_workflows.items()):
                if isinstance(workflow.get("nodes"), list):
                    continue
                if node_id not in workflow:
                    failures.append(
                        f"{where} maps node id {node_id!r}, absent from edit "
                        f"workflow {name!r}. The mapping is shared across "
                        "every committed EDIT workflow so switching "
                        "image_edit_workflow can never discover a broken mapping"
                    )
                else:
                    class_type = workflow[node_id]["class_type"]
                    accepted = CLASS_INPUTS.get(class_type)
                    key = node.get("key", "text")
                    if accepted is None:
                        failures.append(
                            f"edit workflow {name!r} node {node_id!r} has "
                            f"class_type {class_type!r}, not in CLASS_INPUTS"
                        )
                    elif key not in accepted:
                        failures.append(
                            f"{where} writes key {key!r} into edit workflow "
                            f"{name!r} node {node_id!r} ({class_type}), which "
                            f"accepts {sorted(accepted)}"
                        )

    for required in sorted(EDIT_REQUIRED_TYPES - seen_types):
        failures.append(
            f"image_edit_workflow_nodes has no {required!r} entry"
        )

    if not (catalog.get("image_edit_model") or "").strip():
        failures.append("image_edit_model is empty")

    return failures
```

- [ ] **Step 4: Wire it into `main()`, `load_all()`, and the self-check**

Extend `load_all()` to also glob `EDIT_WORKFLOW_DIR`:

```python
def load_all(root: Path) -> tuple[dict, dict[str, dict], dict[str, dict], dict]:
    catalog = yaml.safe_load((root / "inventory" / "group_vars" / "all"
                              / "images.yml").read_text())
    main_vars = yaml.safe_load((root / "inventory" / "group_vars" / "all"
                                / "main.yml").read_text())
    workflows = {}
    for path in sorted((root / "inventory" / "comfyui-workflows").glob("*.json")):
        workflows[path.stem] = json.loads(path.read_text())
    edit_workflows = {}
    for path in sorted((root / "inventory" / "comfyui-edit-workflows").glob("*.json")):
        edit_workflows[path.stem] = json.loads(path.read_text())
    return catalog, workflows, edit_workflows, main_vars
```

Update every call site of `load_all()` and `check_config()` for the new
return arity, and add the edit pass in `main()`:

```python
def main() -> int:
    failures: list[str] = validation_self_check()

    catalog, workflows, edit_workflows, main_vars = load_all(ROOT)
    if not workflows:
        print(f"no workflows found in {WORKFLOW_DIR}", file=sys.stderr)
        return 1
    if not edit_workflows:
        print(f"no edit workflows found in {EDIT_WORKFLOW_DIR}", file=sys.stderr)
        return 1
    failures.extend(check_config(catalog, workflows, main_vars))
    failures.extend(check_edit_config(catalog, edit_workflows))

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        return 1
    print(f"Open WebUI image config: OK ({len(workflows)} generation "
          f"workflow(s), {len(edit_workflows)} edit workflow(s))")
    return 0
```

Add edit-family cases to `validation_self_check()`, following the existing
`VALIDATION_CASES`/mutation-function pattern exactly:

```python
def _edit_negative_prompt_forbidden(catalog, workflows, edit_workflows, main_vars):
    catalog["image_edit_workflow_nodes"].append(
        {"type": "negative_prompt", "key": "text", "node_ids": ["15"]})


def _edit_missing_image_type(catalog, workflows, edit_workflows, main_vars):
    catalog["image_edit_workflow_nodes"] = [
        n for n in catalog["image_edit_workflow_nodes"] if n["type"] != "image"]


def _edit_node_id_absent_from_edit_workflow(catalog, workflows, edit_workflows, main_vars):
    other = good_edit_workflow()
    del other["13"]
    edit_workflows["other"] = other


def _edit_empty_model(catalog, workflows, edit_workflows, main_vars):
    catalog["image_edit_model"] = ""


EDIT_VALIDATION_CASES = (
    ("negative_prompt in edit mapping", _edit_negative_prompt_forbidden,
     "no field on ComfyUIEditImageForm"),
    ("edit mapping missing required image type", _edit_missing_image_type, "image"),
    ("mapped node absent from a non-selected edit workflow",
     _edit_node_id_absent_from_edit_workflow, "other"),
    ("empty image_edit_model", _edit_empty_model, "image_edit_model"),
)
```

Update `validation_self_check()`'s body — it currently takes only the
generation catalog/workflows/main_vars — to also build and check the edit
fixtures with the same baseline-then-mutate loop already used for
`VALIDATION_CASES`, calling `check_edit_config` instead of `check_config` for
each `EDIT_VALIDATION_CASES` entry. Follow the existing function's structure
exactly (baseline assertion first, then one mutation per case, each asserted
to produce a failure containing the expected substring).

- [ ] **Step 5: Run it**

```bash
python scripts/../tests/validate_openwebui_image_config.py 2>&1 | tail -20
```

Wait — this script lives in `tests/` and is invoked directly:

```bash
python tests/validate_openwebui_image_config.py
```

Expected: `Open WebUI image config: OK (2 generation workflow(s), 1 edit workflow(s))`

- [ ] **Step 6: Run the full offline gate**

```bash
make validate
```

Expected: passes. This is the gate this whole feature exists to make
trustworthy — a failure here means a typo would otherwise deploy silently
broken.

- [ ] **Step 7: Commit**

```bash
git add tests/validate_openwebui_image_config.py
git commit -m "feat: validate the Qwen Image Edit mapping as its own workflow family"
```

---

### Task 5: Push the edit config through Open WebUI's admin API

**Files:**
- Modify: `scripts/owui_image_config.py`

**Interfaces:**
- Consumes: `image_edit_*` keys from `images.yml` (Task 3), the edit
  workflow JSON (Task 2).
- Produces: `managed_keys()` returning both families' keys in one dict, so
  `make owui-image-config` pushes generation and editing together in the
  same read-modify-write cycle it already performs.

- [ ] **Step 1: Extend `SHOWABLE` and `managed_keys()`**

```python
SHOWABLE = {
    "ENABLE_IMAGE_GENERATION", "IMAGE_GENERATION_ENGINE", "IMAGE_GENERATION_MODEL",
    "IMAGE_SIZE", "IMAGE_STEPS", "COMFYUI_BASE_URL", "COMFYUI_WORKFLOW_NODES",
    "ENABLE_IMAGE_EDIT", "IMAGE_EDIT_ENGINE", "IMAGE_EDIT_MODEL",
    "IMAGES_EDIT_COMFYUI_BASE_URL", "IMAGES_EDIT_COMFYUI_WORKFLOW_NODES",
}


def managed_keys(catalog: dict, workflow_json: str, edit_workflow_json: str) -> dict[str, object]:
    """The Open WebUI fields this tool owns. Everything else is passed through
    from the live config untouched."""
    return {
        "ENABLE_IMAGE_GENERATION": bool(catalog["image_generation_enabled"]),
        "IMAGE_GENERATION_ENGINE": "comfyui",
        "IMAGE_GENERATION_MODEL": catalog["image_generation_model"],
        "IMAGE_SIZE": catalog["image_size"],
        "IMAGE_STEPS": int(catalog["image_steps"]),
        "COMFYUI_BASE_URL": catalog["comfyui_base_url"],
        "COMFYUI_WORKFLOW": workflow_json,
        "COMFYUI_WORKFLOW_NODES": catalog["image_workflow_nodes"],
        "ENABLE_IMAGE_EDIT": bool(catalog["image_edit_enabled"]),
        "IMAGE_EDIT_ENGINE": "comfyui",
        "IMAGE_EDIT_MODEL": catalog["image_edit_model"],
        "IMAGES_EDIT_COMFYUI_BASE_URL": catalog["comfyui_base_url"],
        "IMAGES_EDIT_COMFYUI_WORKFLOW": edit_workflow_json,
        "IMAGES_EDIT_COMFYUI_WORKFLOW_NODES": catalog["image_edit_workflow_nodes"],
    }
```

`IMAGES_EDIT_COMFYUI_BASE_URL` reuses `comfyui_base_url` — one ComfyUI
instance serves both features, so there is no separate edit-specific host to
track, and the validator's drift check (Task 4) only needs to keep the one
`comfyui_base_url`/`gpu_host_ip` pair in agreement.

- [ ] **Step 2: Update `main()` to load and pass the edit workflow**

```python
    try:
        catalog = yaml.safe_load(CATALOG_PATH.read_text())
        workflow_path = WORKFLOW_DIR / f"{catalog['image_workflow']}.json"
        workflow = json.loads(workflow_path.read_text())
        workflow_json = json.dumps(workflow)
        edit_workflow_path = (ROOT / "inventory" / "comfyui-edit-workflows"
                              / f"{catalog['image_edit_workflow']}.json")
        edit_workflow = json.loads(edit_workflow_path.read_text())
        edit_workflow_json = json.dumps(edit_workflow)
        desired = managed_keys(catalog, workflow_json, edit_workflow_json)
    except (OSError, KeyError, ValueError, yaml.YAMLError) as error:
        print(f"catalog or workflow is unusable: {error}", file=sys.stderr)
        return 3
```

Everything after this point in `main()` — the GET, the diff, the dry-run
report, the POST, the readback assertion — is unchanged; `desired` now simply
carries more keys, and `diff_keys`/`show` already operate generically over
whatever `managed_keys()` returns.

- [ ] **Step 3: Dry-run against the live Open WebUI**

```bash
OWUI_ADMIN_TOKEN=<token from vault.yml, read by hand> \
  python scripts/owui_image_config.py --dry-run
```

Expected: lists the new `ENABLE_IMAGE_EDIT`, `IMAGE_EDIT_ENGINE`,
`IMAGE_EDIT_MODEL`, `IMAGES_EDIT_COMFYUI_BASE_URL`,
`IMAGES_EDIT_COMFYUI_WORKFLOW` (redacted — not in `SHOWABLE`, correctly, since
its length exceeds 200 chars anyway and the existing `show()` truncation
already handles that), `IMAGES_EDIT_COMFYUI_WORKFLOW_NODES` as changing;
does **not** print the token or any other config value. Do not pass
`--dry-run` for the real push — that happens in Task 8, after the runtime
check script exists to prove it worked.

- [ ] **Step 4: Commit**

```bash
git add scripts/owui_image_config.py
git commit -m "feat: push the Qwen Image Edit config alongside generation"
```

---

### Task 6: Write the runtime end-to-end check

**Files:**
- Create: `scripts/image_edit_check.py`
- Modify: `Makefile`

**Interfaces:**
- Consumes: `api_get`/`api_post` from `scripts/owui_image_config.py`
  (already importable — `image_generation_check.py` does the same); the
  live `/api/v1/images/edit` endpoint.
- Produces: exit code 0 (`ok`) / 1 (`broken`) / 2 (`inconclusive`), matching
  `image_generation_check.py`'s tri-state convention exactly, and
  `homelab_image_edit_*` metrics in the same shape as
  `homelab_image_generation_*`.

- [ ] **Step 1: Write the module**

```python
#!/usr/bin/env python3
"""Edit one image end to end and prove OUR workflow produced it.

Generation's runtime check proves itself by asserting an exact output size —
the compiled-in default is 512x512 and ours is 1024x1024, so a size match is
strong evidence our mapping reached ComfyUI. Editing has no such fixed target:
this workflow derives its output size from the input image
(ImageScaleToTotalPixels), so a size assertion here would prove nothing.
Instead this check reads back ComfyUI's own /history for the executed prompt
and asserts the UNETLoader node's unet_name equals IMAGE_EDIT_MODEL -- direct
proof our checkpoint executed, not merely that *an* image came back.

Verdicts are tri-state, same convention as image_generation_check.py:
`inconclusive` means could-not-look and escalates rather than passing.

Design: docs/superpowers/specs/2026-08-27-comfyui-image-editing-design.md
"""

from __future__ import annotations

import argparse
import base64
import os
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zlib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from owui_image_config import api_get, api_post  # noqa: E402

CATALOG_PATH = ROOT / "inventory" / "group_vars" / "all" / "images.yml"
PROMPT = "make the background bright red"


def tiny_png(width: int = 64, height: int = 64,
             rgb: tuple[int, int, int] = (120, 90, 200)) -> bytes:
    """A minimal valid PNG built from the stdlib alone -- no test fixture,
    no PIL dependency, deterministic bytes every run."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff))
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    idat = chunk(b"IDAT", zlib.compress(raw, 9))
    return sig + ihdr + idat + chunk(b"IEND", b"")


def png_size(data: bytes) -> tuple[int, int]:
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def probe(base_url: str, token: str, catalog: dict,
          timeout: int) -> tuple[str, dict[str, float]]:
    expected_model = catalog["image_edit_model"]
    started = time.monotonic()

    image_b64 = base64.b64encode(tiny_png()).decode("ascii")
    payload = {"image": f"data:image/png;base64,{image_b64}", "prompt": PROMPT}

    try:
        result = api_post(base_url, "/api/v1/images/edit", token, payload, timeout)
    except urllib.error.HTTPError as error:
        if error.code == 403:
            return "inconclusive", {}
        return "broken", {"homelab_image_edit_ok": 0.0}
    except (urllib.error.URLError, OSError, ValueError):
        return "inconclusive", {}

    if not isinstance(result, list) or not result:
        print("FAIL: edit returned no image", file=sys.stderr)
        return "broken", {"homelab_image_edit_ok": 0.0}

    url = result[0].get("url", "")
    if url.startswith("/"):
        url = f"{base_url.rstrip('/')}{url}"
    try:
        request = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read()
    except (urllib.error.URLError, urllib.error.HTTPError, OSError):
        return "inconclusive", {}

    try:
        width, height = png_size(data)
    except ValueError:
        print(f"FAIL: returned {len(data)} bytes that are not a PNG",
              file=sys.stderr)
        return "broken", {"homelab_image_edit_ok": 0.0}

    duration = time.monotonic() - started
    metrics = {
        "homelab_image_edit_duration_seconds": round(duration, 2),
        "homelab_image_edit_width": float(width),
    }

    # The direct proof: read ComfyUI's own history for the most recent
    # prompt and confirm OUR checkpoint executed, not merely that an image
    # of some size came back. Best-effort -- history is in-memory and reused
    # across requests, so failing to confirm degrades to a weaker check
    # rather than a hard failure.
    try:
        history = api_get(base_url.replace("192.168.1.32:3007", "192.168.1.40:8188"),
                          "/history", token="", timeout=timeout)
    except Exception:
        history = None
    ran_expected_model = None
    if isinstance(history, dict) and history:
        latest = list(history.values())[-1]
        prompt_graph = latest.get("prompt", [None, None, {}])[2]
        for node in prompt_graph.values():
            if node.get("class_type") == "UNETLoader":
                ran_expected_model = (
                    node.get("inputs", {}).get("unet_name") == expected_model)

    if (width, height) == (0, 0):
        metrics["homelab_image_edit_ok"] = 0.0
        return "broken", metrics

    if ran_expected_model is False:
        print(f"FAIL: ComfyUI's history shows a different UNETLoader model "
              f"than {expected_model!r} -- our mapping did not reach it",
              file=sys.stderr)
        metrics["homelab_image_edit_ok"] = 0.0
        return "broken", metrics

    metrics["homelab_image_edit_ok"] = 1.0
    print(f"image edit OK: {width}x{height} PNG, {len(data)} bytes, "
          f"{duration:.1f}s, model confirmed={ran_expected_model}")
    return "ok", metrics


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://192.168.1.32:3007")
    parser.add_argument("--token-file", metavar="FILE")
    parser.add_argument("--metrics-dir", metavar="DIR")
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()

    if args.token_file:
        token = Path(args.token_file).read_text().strip()
    else:
        token = os.environ.get("OWUI_ADMIN_TOKEN", "").strip()
    if not token:
        print("no admin token: set OWUI_ADMIN_TOKEN or pass --token-file",
              file=sys.stderr)
        return 1

    catalog = yaml.safe_load(CATALOG_PATH.read_text())
    if not catalog.get("image_edit_enabled"):
        print("image_edit_enabled is false in images.yml — the feature is "
              "disabled, so this run could not look and is not an all-clear")
        return 2

    verdict, metrics = probe(args.base_url, token, catalog, args.timeout)

    if args.metrics_dir and metrics:
        lines = "".join(f"{name} {value}\n" for name, value in sorted(metrics.items()))
        try:
            subprocess.run(
                ["homelab-metric-write", "--dir", args.metrics_dir,
                 "--file", "image-edit", "--prefix", "homelab_image_edit",
                 *(["--success"] if verdict == "ok" else [])],
                input=lines, text=True, check=True,
            )
        except (FileNotFoundError, subprocess.CalledProcessError) as error:
            print(f"failed to publish metrics: {error}", file=sys.stderr)

    print(f"verdict={verdict}")
    return {"ok": 0, "broken": 1, "inconclusive": 2}[verdict]


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Add the Makefile target**

```makefile
image-edit-check: ## Edit one image end to end and assert Open WebUI reached our checkpoint
	$(PYTHON) scripts/image_edit_check.py $(ARGS)
```

Insert it directly after the existing `image-gen-check` target, and add
`image-edit-check` to the `.PHONY` line alongside `image-gen-check`.

- [ ] **Step 3: `make validate`**

Expected: passes (this script is not a gate itself, but must not break
syntax/lint checks that scan `scripts/`).

- [ ] **Step 4: Commit**

```bash
git add scripts/image_edit_check.py Makefile
git commit -m "feat: add the Qwen Image Edit end-to-end runtime check"
```

---

### Task 7: Push live and run the end-to-end proof

**Files:** none — this task exercises Tasks 3–6's output against the real
Open WebUI instance. No commit at the end; it is a verification task.

**Interfaces:**
- Consumes: everything from Tasks 1–6.
- Produces: a live, working feature, proven by `make image-edit-check`
  returning `ok`.

- [ ] **Step 1: Confirm the tree is clean and `make validate` passes**

```bash
git status --porcelain
make validate
```

Expected: no output from `git status`, `make validate` exits 0.

- [ ] **Step 2: Push the config**

```bash
OWUI_ADMIN_TOKEN=<token> make owui-image-config
```

Expected: `pushed N key(s) and confirmed by readback`, exit 0. If it exits 2,
stop — the readback disagreeing means Open WebUI rejected or rewrote
something Task 5 assumed it would accept, and that needs to be understood
before continuing, not retried blindly.

- [ ] **Step 3: Confirm nothing about generation broke**

```bash
OWUI_ADMIN_TOKEN=<token> make image-gen-check
```

Expected: `verdict=ok`. This is the regression check — pushing the edit
keys is a whole-object POST (per the generation design's "Secret handling"
section), so this proves the shared push did not clobber a generation key.

- [ ] **Step 4: Run the edit check**

```bash
OWUI_ADMIN_TOKEN=<token> make image-edit-check
```

Expected: `verdict=ok`, an edited PNG at whatever size `ImageScaleToTotalPixels`
produced, with `model confirmed=True`. If `broken`: read the printed FAIL
line first — it is written to name the exact rule that failed (no image, not
a PNG, wrong model in history) rather than just "it did not work."

If this fails with an OOM-shaped symptom (ComfyUI 500s, or `inconclusive`
from a timeout that Task 2 Step 5 did not already surface): the chat model
may be resident. Confirm and clear it:

```bash
curl -s http://192.168.1.40:11434/api/ps
```

If non-empty, `ollama stop <model>` before retrying — per the design (§5),
editing cannot currently coexist with a resident chat model the way
generation does.

---

### Task 8: Wire the nightly check into svc-infra

**Files:**
- Create: `roles/svc_infra/files/homelab-image-edit@.service`
- Create: `roles/svc_infra/files/homelab-image-edit@.timer`
- Modify: `roles/svc_infra/tasks/verify-runner.yml`

**Interfaces:**
- Consumes: `/etc/homelab-owui-admin.token` (already deployed by the
  existing image-gen nightly wiring — reused, not redeployed), the venv at
  `/opt/homelab-iac/.venv`.
- Produces: a `homelab-image-edit@svcops.timer` running nightly, publishing
  `homelab_image_edit_*` metrics the same way `homelab-image-gen@` already
  publishes `homelab_image_generation_*`.

- [ ] **Step 1: Write the service unit**

```ini
# Nightly end-to-end proof that in-chat image EDITING still works.
# Same reasoning as homelab-image-gen@.service, kept separate from it: a red
# edit check and a red generation check are different failures (different
# checkpoint, different code path in Open WebUI), and collapsing them would
# make it unclear which one broke from the alert alone.
[Unit]
Description=homelab-iac in-chat image editing check for %i
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
User=%i
WorkingDirectory=/opt/homelab-iac
ExecStartPre=/usr/bin/test -d /opt/homelab-iac/.venv
ExecStart=/opt/homelab-iac/.venv/bin/python \
    /opt/homelab-iac/scripts/image_edit_check.py \
    --token-file /etc/homelab-owui-admin.token \
    --metrics-dir /opt/homelab/appdata/node-exporter-textfile
# Editing loads a ~28 GB stack cold every night (it cannot stay resident
# alongside a chat model, design §5) against a desktop that may be asleep.
# Longer than image-gen's 900s for that reason.
TimeoutStartSec=1200
UMask=0077
SuccessExitStatus=2
```

Save as `roles/svc_infra/files/homelab-image-edit@.service`.

- [ ] **Step 2: Write the timer unit**

```ini
# Enabled for the runner account by roles/svc_infra/tasks/verify-runner.yml:
#   systemctl enable --now homelab-image-edit@svcops.timer
[Unit]
Description=Run the in-chat image editing check nightly for %i

[Timer]
Unit=homelab-image-edit@%i.service
# 07:00, after image-gen's 06:15 (+ up to 900s randomized delay, + up to
# 900s TimeoutStartSec) has had time to finish. Both checks submit real
# ComfyUI prompts and the two should not race each other on the same GPU.
OnCalendar=*-*-* 07:00:00
RandomizedDelaySec=900
Persistent=true

[Install]
WantedBy=timers.target
```

Save as `roles/svc_infra/files/homelab-image-edit@.timer`.

- [ ] **Step 3: Install and arm it in `verify-runner.yml`**

Add immediately after the existing image-gen block (the one ending at line
367):

```yaml
# ----------------------------------------------- nightly image editing ---
# Same shape as the image-generation block above, one file over: proves
# Open WebUI's edit path reaches Qwen Image Edit specifically, not just that
# generation still works.
- name: Install the nightly image editing check units
  ansible.builtin.copy:
    src: "{{ item }}"
    dest: "/etc/systemd/system/{{ item }}"
    owner: root
    group: root
    mode: "0644"
  loop:
    - homelab-image-edit@.service
    - homelab-image-edit@.timer
  register: image_edit_runner_units

- name: Arm the nightly image editing check timer
  ansible.builtin.systemd:
    name: "homelab-image-edit@{{ verify_runner_user }}.timer"
    enabled: true
    state: started
    daemon_reload: "{{ image_edit_runner_units is changed }}"
```

- [ ] **Step 4: `make validate`**

Expected: passes, including `tests/validate_systemd_units.py`'s
`systemd-analyze verify` pass over the new unit files (per CLAUDE.md, this
now runs locally on TERRA via WSL's systemd 255).

- [ ] **Step 5: Commit**

```bash
git add roles/svc_infra/files/homelab-image-edit@.service \
        roles/svc_infra/files/homelab-image-edit@.timer \
        roles/svc_infra/tasks/verify-runner.yml
git commit -m "feat: run the Qwen Image Edit check nightly on svc-infra"
```

---

### Task 9: Docs, deploy, and close out per the CLAUDE.md workflow

**Files:**
- Modify: `docs/gpu-host.md`
- Modify: `docs/plans/image-editing.md`

**Interfaces:** none — this task is documentation plus the final deploy
sequence from CLAUDE.md's "The change workflow".

- [ ] **Step 1: Record the sourced files and measured VRAM in `docs/gpu-host.md`**

Add a new subsection after "#### The image models installed alongside Pony
and SDXL", following that section's exact table shape (file, destination,
bytes, then a fenced SHA256 block), using the three files from Task 1 and
the measured figures from Task 2 Step 5 (idle baseline, peak, headroom).
State plainly whether it fit with comfortable headroom or was tight, the
same way the existing Flux entry does ("fits, but only just").

- [ ] **Step 2: Update `docs/plans/image-editing.md`'s status**

Replace the `**Status: not implemented.**` line and the "Not investigated"
section (now answered) with a short pointer:

```markdown
**Status: implemented 2026-08-27.** See
[docs/superpowers/specs/2026-08-27-comfyui-image-editing-design.md](../superpowers/specs/2026-08-27-comfyui-image-editing-design.md)
for the design and
[docs/superpowers/plans/2026-08-27-comfyui-image-editing.md](../superpowers/plans/2026-08-27-comfyui-image-editing.md)
for the implementation. Verified working by `make image-edit-check`.
```

Leave the rest of the file's history (the 2026-08-27 split from the video
generation plan) intact — it is still an accurate record of why this page
exists separately from that one.

- [ ] **Step 3: `make validate`, confirm clean tree**

```bash
make validate
git status --porcelain
```

Expected: validate passes; `git status --porcelain` prints nothing once
Steps 1–2 are committed.

- [ ] **Step 4: Commit the docs**

```bash
git add docs/gpu-host.md docs/plans/image-editing.md
git commit -m "docs: record the Qwen Image Edit sourcing, VRAM measurement, and status"
```

- [ ] **Step 5: Final deploy from the clean tree**

```bash
make infra
```

Per CLAUDE.md's svc-infra caveat: expect exactly `changed=3` (the
git-archive-sync tasks) on this first run after a commit. Run it again:

```bash
make infra
```

Expected: `changed=0`. If any task other than those three changed on the
first run, or the second run is not `changed=0`, stop and explain the diff
before merging — do not paper over it by re-running and quoting the second
number.

- [ ] **Step 6: `make verify`**

Expected: passes.

- [ ] **Step 7: Merge to `main`, push, delete the branch**

```bash
git switch main
git merge --ff-only <branch>
git push
git branch -d <branch>
```

Per CLAUDE.md's "Step 8 was skipped for the repo's first 75 commits" — do
not skip it here either.

---

## Self-Review Notes

- **Spec coverage:** §1 (mechanism, negative_prompt/width/height gaps) →
  Task 3/4. §2 (checkpoint) → Task 1. §3 (workflow) → Task 2. §4 (validator
  gap) → Task 4. §5 (VRAM) → Task 2 Steps 4–5, recorded in Task 9. §6
  (catalog shape) → Task 3/5. §7 (runtime check strategy) → Task 6. Nightly
  wiring (implied by "the shape carries over" in the generation design, made
  explicit here) → Task 8.
- **Placeholder scan:** every code block is complete and runnable as
  written, not sketched; every hash, byte count, and URL is one this session
  fetched and verified rather than recalled from training data.
- **Type/name consistency:** `image_edit_workflow_nodes`,
  `image_edit_model`, `image_edit_enabled`, `image_edit_workflow` are used
  identically across Tasks 3, 4, 5, 6. Node IDs `"10"`–`"22"` in Task 2's
  workflow file match the IDs used in Task 3's catalog mapping and Task 4's
  fixtures. `managed_keys()`'s new three-argument signature (Task 5, Step 2)
  matches its call site update in the same step.
