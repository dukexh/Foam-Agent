# AGENTS.md

> This file helps AI agents (Codex, Cursor, Claude Code, Copilot, etc.) understand and work with this codebase.

## What is Foam-Agent?

Foam-Agent is a multi-agent framework that automates CFD (Computational Fluid Dynamics) simulations in **Foundation OpenFOAM v10** ([openfoam.org](https://openfoam.org)) from natural language prompts. It uses LangChain/LangGraph for orchestration, FAISS for RAG-based tutorial retrieval, and supports multiple LLM providers (OpenAI, Anthropic, Bedrock, Ollama).

> **Important:** Foundation v10 remains the default. `FOAMAGENT_OPENFOAM_TARGET=esi-v2006` selects a separate native ESI/OpenCFD v2006 path with its own tutorials and runtime checks. The historical `FOAMAGENT_OPENFOAM_FORK=esi` path remains a best-effort v10-to-ESI translator and is not the native v2006 target.

## Build and Run

```bash
# Environment setup
conda env create -n FoamAgent -f environment.yml
conda activate FoamAgent

# Run a simulation
python foambench_main.py --output ./output --prompt_path ./user_requirement.txt

# Run with custom mesh
python foambench_main.py --output ./output --prompt_path ./user_requirement.txt --custom_mesh_path ./mesh.msh

# Run tests
pytest tests/ -v

# Start MCP server
python -m src.mcp.fastmcp_server --transport http --host 0.0.0.0 --port 7860
```

Requires a sourced OpenFOAM runtime (`$WM_PROJECT_DIR` must be set): Foundation v10 for the default path, or ESI/OpenCFD v2006 when `FOAMAGENT_OPENFOAM_TARGET=esi-v2006`. Python 3.12.9 via Conda.

## Architecture

### Workflow Pipeline (LangGraph StateGraph)

Defined in `src/main.py`. Prompt-generated cases use the normal planning and
generation path; imported cases enter a protected deterministic branch and
then reuse the common local runner and visualization nodes.

```
START -> ENTRY
          |
          +-- prompt -> PLANNER -> MESHING (if needed) -> INPUT_WRITER
          |              -> LOCAL/HPC RUNNER -> REVIEWER -> retry INPUT_WRITER
          |              -> VISUALIZATION (if requested) -> END
          |
          +-- existing case -> CASE_IMPORT -> LOCAL_RUNNER
                               -> REVIEWER (safe non-numeric repair only)
                               -> retry LOCAL_RUNNER or VISUALIZATION -> END
```

Prompt routing decisions are implemented in `src/router_func.py`. Existing
case routing bypasses Planner, Meshing, Input Writer, and LLM-based repair.

### Directory Structure

```
src/
  main.py              # LangGraph workflow definition and entry point
  config.py            # Config dataclass with env var overrides
  utils.py             # GraphState (TypedDict), LLMService (unified LLM interface)
  models.py            # Pydantic models for generated files and plans
  router_func.py       # LLM-based routing decisions
  logger.py            # Structured XML-tagged logging
  nodes/               # LangGraph node functions (thin wrappers calling services)
    planner_node.py
    input_writer_node.py
    meshing_node.py
    local_runner_node.py
    hpc_runner_node.py
    reviewer_node.py
    visualization_node.py
    imported_case_node.py
  services/            # Business logic (where the real work happens)
    plan.py            # Case planning and analysis
    input_writer.py    # OpenFOAM file generation via LLM + RAG
    mesh.py            # Mesh generation (blockMesh / Gmsh conversion)
    run_local.py       # Local OpenFOAM execution
    run_hpc.py         # HPC job submission
    review.py          # Error diagnosis and fix planning
    visualization.py   # PyVista-based post-processing
    case_import.py     # Complete controlled existing-case import service
    allrun_commands.py # Shared Allrun command inspection helpers
    openfoam_commands.py # Shared mesh-command policy
    case_paths.py      # Shared generated path validation
    output_safety.py   # Output ownership and overwrite protection
  mcp/                 # FastMCP server exposing workflow as tools
  translation/         # Legacy Foundation-to-ESI translation compatibility
  openfoam_target.py   # Native target selection and runtime/corpus checks
database/
  foundation-v10/      # Foundation v10 raw data and FAISS indices
  esi-v2006/           # Native ESI/OpenCFD v2006 raw data and FAISS indices
  script/               # Shared corpus parsers and FAISS builders
tests/                 # Focused pytest regressions and manual integration scripts
docker/                # Dockerfile for containerized deployment
```

### Key Abstractions

- **`GraphState`** (`src/utils.py`): TypedDict threaded through all workflow nodes. Contains user requirement, case metadata, generated files, error logs, loop count.
- **`LLMService`** (`src/utils.py`): Unified LLM interface supporting OpenAI, Anthropic, Bedrock, Ollama. Provides `invoke()` and `structure_output()` (Pydantic-validated).
- **`Config`** (`src/config.py`): Global config dataclass. Every field can be overridden via `FOAMAGENT_*` env vars.
- **Pydantic models** (`src/models.py`): `FoamPydantic`/`FoamfilePydantic` for generated files, `RewritePlan` for error fixes, `CaseSummaryModel` for case metadata.
- **Native targets** (`src/openfoam_target.py`): Foundation v10 is the default; native ESI/OpenCFD v2006 is opt-in and uses an isolated corpus and runtime guard.
- **Controlled import service** (`src/services/case_import.py`): Materialises a source case into read-only `original/` and writable `work/` trees, validates an allowed Allrun plan, and permits only numeric-invariant repairs.

### Design Patterns

1. **Service-oriented**: Nodes in `src/nodes/` are thin orchestration wrappers. All logic lives in `src/services/`.
2. **Error correction loop**: Runner detects errors -> Reviewer diagnoses via LLM -> Input Writer rewrites targeted files -> re-run (up to `max_loop` iterations).
3. **RAG retrieval**: FAISS indices built from OpenFOAM tutorials provide reference cases to the input writer.
4. **Two generation modes** (`config.input_writer_generation_mode`):
   - `sequential_dependency` (default): Files generated in order with cross-file context.
   - `parallel_no_context`: All files generated independently (faster, relies on retry loop).
5. **Target-scoped corpora**: Foundation v10 and native ESI v2006 use separate raw and FAISS data under `database/<target>/`.

## Environment Variables

| Variable | Purpose |
|----------|---------|
| `FOAMAGENT_MODEL_PROVIDER` | LLM provider: `openai`, `openai-codex`, `anthropic`, `bedrock`, `ollama` |
| `FOAMAGENT_MODEL_VERSION` | Model identifier (e.g., `claude-opus-4-6`, `gpt-5.3-codex`) |
| `FOAMAGENT_EMBEDDING_PROVIDER` | Embedding backend: `openai`, `huggingface`, `ollama` |
| `FOAMAGENT_EMBEDDING_MODEL` | Embedding model (default: `Qwen/Qwen3-Embedding-0.6B`) |
| `FOAMAGENT_OPENFOAM_FORK` | Legacy fork routing: `foundation` or generic translated `esi` |
| `FOAMAGENT_OPENFOAM_TARGET` | Explicit native target: `foundation-v10` or `esi-v2006` |
| `FOAMAGENT_ESI_V2006_DATABASE_PATH` | Optional isolated ESI v2006 tutorial/FAISS corpus root |
| `FOAMAGENT_HPC_OPENFOAM_BASHRC` | Trusted v2006 `etc/bashrc` used by native HPC job scripts |
| `OPENAI_API_KEY` | Required for `openai` provider |
| `ANTHROPIC_API_KEY` | Required for `anthropic` provider |
| `WM_PROJECT_DIR` | OpenFOAM installation path (required at runtime) |

## Common Tasks

### Adding a new LLM provider
Extend `LLMService` in `src/utils.py`. Follow the pattern of existing providers (each has an `if` branch in the constructor).

### Adding a new workflow node
1. Create service logic in `src/services/`.
2. Create a thin node wrapper in `src/nodes/`.
3. Wire it into the StateGraph in `src/main.py`.

### Modifying file generation
The input writer logic is in `src/services/input_writer.py`. It uses RAG context from FAISS indices and LLM calls to generate OpenFOAM configuration files.

### Rebuilding FAISS indices
Only needed if OpenFOAM tutorials change:
```bash
python init_database.py --openfoam_path $WM_PROJECT_DIR --force
```

## Things to Watch Out For

- **Do not regenerate FAISS indices** unless you have a specific reason. The pre-built Foundation indices in `database/foundation-v10/faiss/` are correct and ready to use.
- **Foundation OpenFOAM v10 must be sourced** for the default path. Native `esi-v2006` requires an ESI/OpenCFD v2006 environment (`WM_PROJECT_VERSION=v2006`) and a separately built v2006 corpus under `database/esi-v2006/` or the configured override.
- **The error correction loop** can run up to 25 iterations. When modifying the reviewer or input writer, consider the impact on convergence.
- **`GraphState` is mutable** and passed by reference through the entire pipeline. Be careful about unintended side effects when modifying state fields.
