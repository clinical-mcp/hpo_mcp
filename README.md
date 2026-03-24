# HPO MCP Server (Model-Agnostic)

This repository provides a **universal MCP server** for Human Phenotype Ontology (HPO) lookup.

It exposes stable tools that work with any MCP-compatible LLM client (Claude Desktop, OpenAI-compatible MCP clients, local clients, etc.) without needing model-specific server code.

## Features

- One server script: `hpo_mcp.py`
- Stable MCP tools:
  - `search_hpo_terms(query: str)`
  - `get_hpo_term_details(hpo_id: str)`
- Supports both transports:
  - `stdio` (local MCP client integration)
  - `sse` (remote/http use, can be exposed via ngrok)
- Configurable via environment variables

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

If `HPO_JSON_PATH` is not set, the server looks for:
1. `hp.json` next to `hpo_mcp.py`
2. `hp.json` in the current working directory

If no local file is found and `HPO_MCP_AUTO_DOWNLOAD=true`, it will download `hp.json` from `HPO_JSON_URL`.

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

### Build image

From the folder containing `Dockerfile`:

```bash
docker build -t hpo-mcp:latest .
```

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

---

## Notes

- If using SSE remotely, expose port with ngrok or another tunnel and use the `/sse` endpoint.
- Ensure your `hp.json` source/license allows redistribution if you commit it to a public repo.
