# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    MCPGW_HOST=0.0.0.0 MCPGW_PORT=8787

WORKDIR /app
COPY pyproject.toml README.md ./
COPY mcp_gateway ./mcp_gateway
RUN pip install .

# Example backends + config are included so the image can run the demo out of the box.
COPY examples ./examples

RUN useradd --system --uid 10001 gateway && mkdir -p /app/var && chown gateway /app/var
USER gateway

EXPOSE 8787
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8787/healthz').status == 200 else 1)"

ENTRYPOINT ["mcp-gateway"]
CMD ["serve", "--config", "examples/gateway.yaml"]
