# Azure Container Apps - Tokan Enterprise Gateway
# Production Python 3.11 image with tree-sitter + vector cache + router
FROM python:3.11-slim

WORKDIR /app

# System dependencies for tree-sitter / ONNX
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ make libffi-dev redis-tools curl \
    && rm -rf /var/lib/apt/lists/*

# Install core Python dependencies
COPY pyproject.toml .
RUN pip install --no-cache-dir -e . || pip install --no-cache-dir . || true

# Install optional enterprise layers
RUN pip install --no-cache-dir tree-sitter fastembed onnxruntime redis || true

# Copy source
COPY tokan/ ./tokan/
COPY benchmarks/ ./benchmarks/
COPY examples/ ./examples/
COPY tests/ ./tests/

EXPOSE 4040

# Health readiness probe endpoint
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:4040/healthz || exit 1

# Launch gateway
CMD ["python", "-m", "tokan.cli", "serve", "--host", "0.0.0.0", "--port", "4040"]
