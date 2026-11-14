# Mirsad MCP server — container image.
# Catalogs (e.g. Glama) build this image, start the server over stdio,
# and introspect its tools. The CMD must launch the server locally —
# no URLs, no remote endpoints.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Local-first: the memory database lives inside the container.
ENV MIRSAD_TRANSPORT=stdio
ENV MIRSAD_DB=/app/mirsad.db

CMD ["python", "-m", "mirsad.mcp_server"]
