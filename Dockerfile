# Use official Python Alpine image as base (smaller footprint, fewer vulnerabilities)
FROM python:3.11-alpine

# Set working directory
WORKDIR /app

# Create a non-root user for security (Alpine syntax)
RUN adduser -D -g '' appuser

# Copy project files needed for install
COPY pyproject.toml README.md LICENSE ./
COPY src/ ./src/

# Install the application and its dependencies (non-editable for runtime)
RUN pip install --no-cache-dir .

# Change to non-root user
USER appuser

# Health check: verify the MCP module can be imported and entry point resolves
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import edupage_mcp; print('healthy')" || exit 1

ENTRYPOINT ["python", "-m", "edupage_mcp"]