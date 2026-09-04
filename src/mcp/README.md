# Foam-Agent MCP Server

Expose OpenFOAM CFD simulation as tools for any AI coding assistant via [MCP (Model Context Protocol)](https://modelcontextprotocol.io/).

> **OpenFOAM version:** This server targets **Foundation OpenFOAM v10** ([openfoam.org](https://openfoam.org)) by default. `FOAMAGENT_OPENFOAM_FORK=esi` remains the legacy best-effort translation path for generated files. Set `FOAMAGENT_OPENFOAM_TARGET=esi-v2006` to use the supported native ESI/OpenCFD v2006 target with its isolated corpus, native conventions, and target-specific runtime checks. Native v2006 requires a matching runtime and corpus.

## Quick Start

### 1. Install

```bash
# Clone and install
git clone https://github.com/csml-rpi/Foam-Agent.git
cd Foam-Agent
pip install -e .
```

Or with conda (full environment including PyTorch, FAISS, etc.):

```bash
conda env create -f environment.yml
conda activate FoamAgent
pip install -e .
```

### 2. Register with your AI tool (one command)

**Claude Code:**
```bash
claude mcp add foamagent -- foamagent-mcp
```

**Cursor:**
Add to `.cursor/mcp.json`:
```json
{
  "mcpServers": {
    "foamagent": {
      "command": "foamagent-mcp"
    }
  }
}
```

**Windsurf / Other MCP-compatible tools:**
```json
{
  "mcpServers": {
    "foamagent": {
      "command": "foamagent-mcp"
    }
  }
}
```

**HTTP mode** (for web clients or remote access):
```bash
foamagent-mcp --transport http --host 0.0.0.0 --port 7860
```

### 3. Configure LLM provider (optional)

Set environment variables to choose your LLM backend:

```bash
export FOAMAGENT_MODEL_PROVIDER=anthropic          # openai, anthropic, bedrock, ollama
export FOAMAGENT_MODEL_VERSION=claude-sonnet-4-6   # model identifier
export ANTHROPIC_API_KEY=sk-ant-...                # API key for your provider
```

## Available MCP Tools

Foam-Agent generates output following **Foundation OpenFOAM v10** conventions by default.
Native ESI/OpenCFD v2006 is selected with `FOAMAGENT_OPENFOAM_TARGET=esi-v2006` and uses
the isolated v2006 corpus and native conventions. The legacy
`FOAMAGENT_OPENFOAM_FORK=esi` setting remains a separate best-effort translation path.

| Tool | Description |
|------|-------------|
| `plan` | Analyze requirements and plan a case using references for the active OpenFOAM target |
| `input_writer` | Generate OpenFOAM configuration files using the active target; legacy `fork=esi` translation remains best-effort |
| `run` | Execute the local case with target-specific runtime selection and error collection |
| `review` | Analyze simulation errors using references for the active OpenFOAM target |
| `apply_fixes` | Rewrite OpenFOAM files using the active target convention |
| `visualization` | Generate PyVista visualization of simulation results |

## Typical Workflow

Once registered, ask your AI assistant naturally:

> "Simulate lid-driven cavity flow at Re=1000"

The assistant will call the tools in sequence:
1. **plan** - Parse requirements, select solver, generate subtasks
2. **input_writer** - Generate all OpenFOAM files
3. **run** - Execute the simulation
4. **review + apply_fixes** - Fix errors if any (automatic retry loop)
5. **visualization** - Render results

## Prerequisites

- **Python 3.10+** with dependencies installed
- **Foundation OpenFOAM v10** ([openfoam.org](https://openfoam.org)) installed and available in PATH for the default runtime path, or a matching **ESI/OpenCFD v2006** runtime when `FOAMAGENT_OPENFOAM_TARGET=esi-v2006` is selected.
- An LLM API key (OpenAI, Anthropic, or local via Ollama)

## Architecture

```
AI Tool (Claude Code / Cursor / ...)
    ↓ MCP protocol (stdio or HTTP)
foamagent-mcp (this server)
    ↓
Service Layer (src/services/*.py)
    ↓
OpenFOAM + LLM Services
```

## Advanced Configuration

| Environment Variable | Purpose | Default |
|---------------------|---------|---------|
| `FOAMAGENT_MODEL_PROVIDER` | LLM backend | `openai-codex` |
| `FOAMAGENT_MODEL_VERSION` | Model identifier | `gpt-5.3-codex` |
| `FOAMAGENT_EMBEDDING_PROVIDER` | Embedding backend | `huggingface` |
| `FOAMAGENT_EMBEDDING_MODEL` | Embedding model | `Qwen/Qwen3-Embedding-0.6B` |
| `FOAMAGENT_OPENFOAM_FORK` | OpenFOAM target fork for generated files: `foundation` or `esi` | `foundation` |
| `FOAMAGENT_OPENFOAM_TARGET` | Explicit native target: `foundation-v10` or `esi-v2006`; v2006 bypasses legacy ESI translation | — |
| `FOAMAGENT_ESI_V2006_DATABASE_PATH` | Optional isolated v2006 tutorial/FAISS corpus root | `database/esi-v2006` |
| `FOAMAGENT_HPC_OPENFOAM_BASHRC` | Trusted v2006 `etc/bashrc` sourced in native HPC job scripts | — |
| `OPENAI_API_KEY` | OpenAI API key | — |
| `ANTHROPIC_API_KEY` | Anthropic API key | — |

## Troubleshooting

**Import errors:** Ensure you ran `pip install -e .` from the repo root.

**Database errors:** Target-scoped FAISS indices ship pre-built in
`database/foundation-v10/` and `database/esi-v2006/`. If a target corpus is
missing, rebuild the selected corpus with:
```bash
python init_database.py --openfoam_path $WM_PROJECT_DIR --openfoam_target foundation-v10 --force
```

**OpenFOAM not found:** The default validated runtime path requires Foundation OpenFOAM v10 ([openfoam.org](https://openfoam.org)). Legacy generic ESI uses `FOAMAGENT_OPENFOAM_FORK=esi`; native v2006 uses `FOAMAGENT_OPENFOAM_TARGET=esi-v2006`, a v2006 target corpus, and `WM_PROJECT_VERSION=v2006`. For a native HPC run set `FOAMAGENT_HPC_OPENFOAM_BASHRC` to the compute-node v2006 `etc/bashrc`. Build the matching image with:
```bash
python scripts/build_docker_image.py
python scripts/build_docker_image.py --openfoam-target esi-v2006
docker run -it -p 7860:7860 foamagent:foundation-v10 foamagent-mcp --transport http
```
