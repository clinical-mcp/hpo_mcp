# HPO MCP Server (Model-Agnostic)

This repository provides a **universal MCP server** for Human Phenotype Ontology (HPO) lookup.

It exposes stable tools that work with any MCP-compatible LLM client (Claude Desktop, OpenAI-compatible MCP clients, local clients, etc.) without needing model-specific server code.

## Features

- One server script: `hpo_mcp.py`
- Stable MCP tools:
  - `search_hpo_terms(query: str)`
  - `get_hpo_term_details(hpo_id: str)`
  - `refresh_hpo_data(force: bool = true)`
  - `get_hpo_parents(hpo_id: str)`
  - `get_hpo_children(hpo_id: str)`
  - `get_hpo_ancestors(hpo_id: str, max_depth: int | null = null)`
  - `get_hpo_descendants(hpo_id: str, max_depth: int | null = null)`
  - `map_clinical_text_to_hpo(text: str, limit: int = 20)`
  - `suggest_more_specific_terms(hpo_id: str, query: str | null = null, limit: int = 15)`
  - `suggest_broader_terms(hpo_id: str, limit: int = 15)`
  - `compare_hpo_terms(hpo_ids: list[str])`
  - `validate_hpo_ids(hpo_ids: list[str])`
  - `get_hpo_subontology(root_hpo_id: str, max_depth: int = 2)`
- Supports both transports:
  - `stdio` (local MCP client integration)
  - `sse` (remote/http use, can be exposed via ngrok)
- Configurable via environment variables
- Hybrid HPO search:
  - exact/normalized lexical matching for precision
  - local TF-IDF/ngram vector search for typo-tolerant approximate matching
  - optional embedding-based semantic search with `sentence-transformers`

---

## Requirements

- Python 3.10+
- HPO data file (`hp.json`) from HPO/OBO format (or equivalent list format)

Install dependencies:

```bash
pip install -r requirements.txt
```

---

## Configuration

You can set these environment variables:

- `HPO_MCP_SERVER_NAME` (default: `HPO-MCP-Universal`)
- `HPO_MCP_HOST` (default: `127.0.0.1`)
- `HPO_MCP_PORT` (default: `8000`)
- `HPO_MCP_TRANSPORT` (`stdio` or `sse`, default: `sse`)
- `HPO_JSON_PATH` (optional absolute/relative path to `hp.json`)
- `HPO_JSON_URL` (default: `http://purl.obolibrary.org/obo/hp.json`)
- `HPO_MCP_AUTO_DOWNLOAD` (default: `true`; download if missing)
- `HPO_MCP_REFRESH_ON_START` (default: `false`; force redownload each startup)
- `HPO_MCP_MAX_DATA_AGE_HOURS` (default: `24`; auto-refresh stale local `hp.json` on startup; set `0` to disable age-based refresh)
- `HPO_MCP_SEARCH_MODE` (`hybrid`, `lexical`, or `vector`; default: `hybrid`)
- `HPO_MCP_SEARCH_LIMIT` (default: `15`)
- `HPO_MCP_VECTOR_MIN_SCORE` (default: `0.08`)
- `HPO_MCP_VECTOR_BACKEND` (`tfidf`, `semantic`, or `auto`; default: `tfidf`)
- `HPO_MCP_SEMANTIC_MODEL` (default: `sentence-transformers/all-MiniLM-L6-v2`)

If `HPO_JSON_PATH` is not set, the server looks for:
1. `hp.json` next to `hpo_mcp.py`
2. `hp.json` in the current working directory

If no local file is found and `HPO_MCP_AUTO_DOWNLOAD=true`, it will download `hp.json` from `HPO_JSON_URL`.

With the defaults, an existing local `hp.json` is refreshed on startup when it is at least 24 hours old. Set `HPO_MCP_REFRESH_ON_START=true` to redownload on every startup, or call the MCP tool `refresh_hpo_data(force=true)` to refresh immediately and rebuild the search indexes without restarting.

---

## Search behavior

`search_hpo_terms(query)` uses `HPO_MCP_SEARCH_MODE=hybrid` by default.

Hybrid search keeps deterministic exact HPO ID/name/synonym matches at the top, then adds vector-ranked matches over the term name, synonyms, and definition text. The default vector backend is dependency-free TF-IDF with word, phrase, and character n-gram features.

For true embedding-based semantic search, install the optional dependency:

```bash
pip install sentence-transformers
# or:
pip install -r requirements-semantic.txt
```

Then run with:

```cmd
set HPO_MCP_VECTOR_BACKEND=semantic && python hpo_mcp.py
```

Use `HPO_MCP_VECTOR_BACKEND=auto` to try semantic embeddings and fall back to the local TF-IDF vector index if the model/dependency is unavailable.

---

## Ontology navigation tools

The server builds parent/child graph indexes from OBO JSON `edges` when available. This enables:

- direct navigation: `get_hpo_parents`, `get_hpo_children`
- broader/narrower expansion: `get_hpo_ancestors`, `get_hpo_descendants`
- compact trees for UI/context: `get_hpo_subontology`
- clinical mapping: `map_clinical_text_to_hpo`
- ID hygiene: `validate_hpo_ids`
- relatedness checks: `compare_hpo_terms`

---

## Run

### Windows (cmd)

SSE mode:

```cmd
set HPO_MCP_TRANSPORT=sse && python hpo_mcp.py
```

stdio mode:

```cmd
set HPO_MCP_TRANSPORT=stdio && python hpo_mcp.py
```

Using a custom data path:

```cmd
set HPO_JSON_PATH=C:\path\to\hp.json && python hpo_mcp.py
```

---

## Example MCP Client Config (stdio)

Use this as a reference and adapt to your MCP client format:

```json
{
  "mcpServers": {
    "hpo": {
      "command": "python",
      "args": ["C:/path/to/hpo_mcp.py"],
      "env": {
        "HPO_MCP_TRANSPORT": "stdio",
        "HPO_JSON_PATH": "C:/path/to/hp.json"
      }
    }
  }
}
```

---

## Quick Test

After starting the server, test in your client by calling:

- `search_hpo_terms("seizure")`
- `get_hpo_term_details("HP:0001250")`
- `refresh_hpo_data(true)`
- `get_hpo_children("HP:0001250")`
- `map_clinical_text_to_hpo("developmental delay, seizures, short stature")`

---

## Expose with ngrok (quick setup)

Use this when you want a public URL to your local SSE MCP server.

1. Start the server in SSE mode:

```cmd
set HPO_MCP_TRANSPORT=sse && python hpo_mcp.py
```

2. In a second terminal, expose your local port (default `8000`):

```cmd
ngrok http 8000
```

3. Copy the HTTPS forwarding URL from ngrok, for example:

`https://abcd-1234.ngrok-free.app`

4. Use this MCP endpoint in your LLM client:

`https://abcd-1234.ngrok-free.app/sse`

Keep both the Python server process and ngrok process running.

---

## Hosting (always-on option)

For persistent use, deploy on a VM/container platform (Render, Railway, Fly.io, Azure, AWS, etc.):

- run `hpo_mcp.py` with `HPO_MCP_TRANSPORT=sse`
- provide `HPO_JSON_PATH` to your `hp.json`
- expose port via HTTPS (or put behind reverse proxy)
- configure your client to use `https://<your-domain>/sse`

ngrok is usually best for development/testing, while cloud hosting is better for production uptime.

---

## Docker

This repo includes a `Dockerfile` and `.dockerignore`.

### Install as a Docker container

You can run this project directly as a Docker container in either of these ways:

#### Option 1: Pull a prebuilt image from GitHub Container Registry

If a published image is available, pull it with:

```bash
docker pull ghcr.io/clinical-mcp/hpo-mcp:latest
```

Then run it:

```bash
docker run --rm -p 8000:8000 \
  -v hpo_mcp_data:/data \
  -e HPO_MCP_TRANSPORT=sse \
  -e HPO_MCP_HOST=0.0.0.0 \
  -e HPO_MCP_PORT=8000 \
  -e HPO_JSON_PATH=/data/hp.json \
  -e HPO_MCP_AUTO_DOWNLOAD=true \
  -e HPO_MCP_REFRESH_ON_START=false \
  ghcr.io/clinical-mcp/hpo-mcp:latest
```

#### Option 2: Build the image locally from this repository

Clone the repository and build it locally:

```bash
git clone https://github.com/clinical-mcp/hpo_mcp.git
cd hpo_mcp
docker build -t hpo-mcp:latest .
```

Then run it:

```bash
docker run --rm -p 8000:8000 \
  -v hpo_mcp_data:/data \
  -e HPO_MCP_TRANSPORT=sse \
  -e HPO_MCP_HOST=0.0.0.0 \
  -e HPO_MCP_PORT=8000 \
  -e HPO_JSON_PATH=/data/hp.json \
  -e HPO_MCP_AUTO_DOWNLOAD=true \
  -e HPO_MCP_REFRESH_ON_START=false \
  hpo-mcp:latest
```

#### What this does

- publishes the MCP server on port `8000`
- stores `hp.json` in `/data` so it persists across restarts
- serves the SSE endpoint at `/sse`

After the container starts, the MCP endpoint is:

```text
http://localhost:8000/sse
```

### Build image

From the folder containing `Dockerfile`:

```bash
docker build -t hpo-mcp:latest .
```

If you want to build directly from GitHub instead of cloning first:

```bash
docker build -t hpo-mcp:latest https://github.com/clinical-mcp/hpo_mcp.git
```

> Note: `https://github.com/clinical-mcp/hpo_mcp.git` is the **source repository URL**.
> Docker/Unraid typically wants a **container image repository** (for example Docker Hub or GHCR),
> not a Git URL, unless you are explicitly building the image from source.

### Run (persist data, download if missing)

This mounts a host folder to `/data` so `hp.json` is cached between restarts.

```bash
docker run --rm -p 8000:8000 \
  -v hpo_mcp_data:/data \
  -e HPO_MCP_TRANSPORT=sse \
  -e HPO_MCP_AUTO_DOWNLOAD=true \
  -e HPO_MCP_REFRESH_ON_START=false \
  hpo-mcp:latest
```

### Run (force update `hp.json` on every restart)

```bash
docker run --rm -p 8000:8000 \
  -v hpo_mcp_data:/data \
  -e HPO_MCP_TRANSPORT=sse \
  -e HPO_MCP_AUTO_DOWNLOAD=true \
  -e HPO_MCP_REFRESH_ON_START=true \
  hpo-mcp:latest
```

### Endpoint

When running in Docker SSE mode, endpoint is:

`http://localhost:8000/sse`

If tunneling with ngrok, use:

`https://<your-ngrok-domain>/sse`

### Unraid install

If you want to host this MCP server on an Unraid server, the easiest approach is to run it as a custom Docker container.

#### Option 1: Use a published Docker image

If you publish this image to Docker Hub or GitHub Container Registry, use that image name in Unraid's **Repository** field.

Examples:

```text
yourdockerhubuser/hpo-mcp:latest
ghcr.io/clinical-mcp/hpo-mcp:latest
```

#### Option 2: Build from the GitHub repository

If you do not have a published image yet, first build it from source:

```bash
docker build -t hpo-mcp:latest https://github.com/clinical-mcp/hpo_mcp.git
```

You can then tag and push it to a registry, or use the locally built image on the Docker host where it was built.

#### Unraid container settings

In Unraid:

1. Go to **Docker** -> **Add Container**.
2. Set the container to use either your published image or your locally available image.
3. Use these recommended settings:

- **Network Type:** `bridge`
- **Port mapping:** host `8000` -> container `8000`
- **AppData path:** map `/mnt/user/appdata/hpo-mcp` -> `/data`

Add these environment variables:

```text
HPO_MCP_TRANSPORT=sse
HPO_MCP_HOST=0.0.0.0
HPO_MCP_PORT=8000
HPO_JSON_PATH=/data/hp.json
HPO_MCP_AUTO_DOWNLOAD=true
HPO_MCP_REFRESH_ON_START=false
```

On first startup, the container will try to download `hp.json` into `/data` if it is not already present.

#### Unraid endpoint

After the container starts, the MCP SSE endpoint should be available at:

```text
http://<your-unraid-ip>:8000/sse
```

For internet-facing access, it is recommended to put this behind a reverse proxy or tunnel and use:

```text
https://<your-domain>/sse
```

---

## Notes

- If using SSE remotely, expose port with ngrok or another tunnel and use the `/sse` endpoint.
- Ensure your `hp.json` source/license allows redistribution if you commit it to a public repo.
