# Use official Python slim image as base
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Create a non-root user for security
RUN adduser --disabled-password --gecos '' appuser
EXPOSE 8000

# Copy project files needed for install
COPY pyproject.toml README.md LICENSE ./
COPY src/ ./src/

# Install the application and its dependencies (non-editable for runtime)
RUN pip install --no-cache-dir .

# Change to non-root user
USER appuser

ENTRYPOINT ["python", "-m", "edupage_mcp"]