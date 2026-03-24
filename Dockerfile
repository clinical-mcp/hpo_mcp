FROM python:3.11-slim

WORKDIR /app

# Install Python dependencies first (better layer caching)
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy app files
COPY hpo_mcp.py ./
COPY README.md ./

# Data directory (can be mounted as a volume)
RUN mkdir -p /data

# Defaults suitable for containerized SSE serving
ENV HPO_MCP_TRANSPORT=sse \
    HPO_MCP_HOST=0.0.0.0 \
    HPO_MCP_PORT=8000 \
    HPO_JSON_PATH=/data/hp.json \
    HPO_MCP_AUTO_DOWNLOAD=true \
    HPO_MCP_REFRESH_ON_START=false

EXPOSE 8000

CMD ["python", "hpo_mcp.py"]
