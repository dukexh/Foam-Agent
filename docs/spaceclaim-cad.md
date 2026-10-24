# SpaceClaim CAD preparation (PR 1)

Foam-Agent can prepare a SpaceClaim `.scdoc` from CAD intent without generating a mesh or
running a solver. The WSL graph owns preparation, visual object planning, native-step state,
human confirmation and recovery. A task-local Windows worker runs Ansys SpaceClaim 2024 R1.
All worker code comes from this repository; a separate CFD-Agent checkout is not required.

Supported operations include volume extraction, explicit reuse of existing fluid bodies,
retaining all valid fluid candidates, user-requested merge groups, and named boundary groups.
Ambiguous selections ask for clarification. Native selection and saved-CAD topology/grouping
are checked by code. Operation semantic reviews and automatic repairs are deferred to PR 3.
Fluent and OpenFOAM mesh conversion are deferred to PR 2.

## Requirements

Use the existing Foam-Agent WSL Python environment. Install Windows Python with `pydantic>=2`
and `Pillow`; it does not need LangChain, PyFluent or this project installed. A Windows venv
outside the source checkout is suitable. SpaceClaim 2024 R1 and its license must be available.
Windows interop must be enabled in WSL.

Set these in the WSL shell (example paths; use your installation):

```bash
export FOAMAGENT_WINDOWS_PYTHON='C:/PythonEnvs/foam-cad/Scripts/python.exe'
export FOAMAGENT_ANSYS_ROOT='E:/Program Files/ANSYS Inc/v241'
export FOAMAGENT_CAD_RUNTIME='C:/Users/Public/FoamAgentCAD'
export FOAMAGENT_VISION_PROVIDER='openai-codex'
export FOAMAGENT_VISION_MODEL='<vision-capable model available to your account>'
```

If vision settings are omitted, the main provider/model is used. Image input is checked with
a real image request. Unsupported models/providers fail explicitly. No model is upgraded
automatically. Provider credentials use Foam-Agent's existing authentication; no credentials
are sent to Windows or stored in task state. Geometry pictures are sent to the selected model.
The existing Codex transport uses tiktoken; a persistent `TIKTOKEN_CACHE_DIR` avoids downloading
the tokenizer again after WSL temporary directories are cleared.

## Start a CAD-only task

```bash
python foambench_main.py \
  --geometry_path '/mnt/d/CAD inputs/model.scdoc' \
  --prompt_path '/mnt/d/CAD inputs/cad-request.txt' \
  --output /home/<user>/cad-runs/example \
  --stop-after cad
```

`python src/main.py` accepts the same arguments, using `--output_dir` instead of `--output`.
Windows drive paths are accepted for the CAD input; output directories follow existing
Foam-Agent rules. The input is copied to an editable task-local document, including when
the original is read-only. A CAD task cannot also import a case or external mesh.

Describe the reference view, desired openings and inner-wall seed, whether the input is
already a fluid domain, any required merging, and boundary names/roles. For example:

```text
The supplied CAD is already the fluid domain. Keep both fluid bodies separate.
Using the isometric view, name the left opening inlet and the right opening outlet.
Assign all remaining boundary faces to walls.
```

GUI mode is the default. The first implementation has one CAD file per task and single-task
native execution. It does not require an OpenFOAM runtime/corpus or a Fluent installation.

## Confirm, pause, cancel and resume

The process always asks for explicit CAD confirmation after preparing the groups.
An empty answer does not approve. Inspect or edit the displayed working CAD in SpaceClaim.

- `confirm`: save the live document, reread the actual geometry, validate and export.
- `save`: save the live document and exit while leaving the confirmation pending.
- `exit`: exit with the checkpoint retained; save any editor changes yourself first.
- `cancel`: end this task and retain its existing files.

If a saved group has been renamed, the CLI asks for missing roles before export. Every face
must belong to exactly one nonempty group, and the saved document must contain valid
positive-volume fluid solids. A failed live-document save never silently falls back to a
stale disk copy. If the editor was closed, the task fails visibly. Choose `reopen` at that
failure to explicitly reopen the saved CAD and establish a new editor/save connection; then
inspect and confirm again. Unsaved changes lost when the old editor closed cannot be restored.

```bash
python foambench_main.py --resume /home/<user>/cad-runs/example
```

The resume command displays the saved pause or failure. At a failure, `retry` explicitly
retries the failed native step; it does not invoke a repair model. `clarify` accepts added
instructions and explicitly reconsiders the affected geometry/body/boundary plan. A task
interrupted during an operation asks for explicit retry on resume. Clarification replies
rerun the affected planner with the added text. If a previous native operation was interrupted,
retry starts from that step's prior document rather than its partially written output.
Environment model/Python/Ansys overrides take precedence over the saved settings on resume.

When no interactive terminal is attached, a pause is returned without approval. Exit codes:
`0` for completed CAD or explicit cancellation, `3` for a pending pause, `2` for a recorded
failure. Other argument/runtime errors retain normal CLI error behavior.

## Results

The WSL output contains `cad-state.json`, `cad-result.json` on completion, `artifacts/confirmed.scdoc`,
`artifacts/confirmed-catalog.json`, archived image evidence, model audit records, operation
receipts and logs. The final status is `cad_ready`, not mesh or simulation success. Catalog
coordinates and volumes use metres/metres cubed; no source length unit is guessed or mesh
scaling performed. Windows native task files remain available for continued editing/recovery.

Source provenance and the retained MIT license are in `src/services/cad/PROVENANCE.md`.
