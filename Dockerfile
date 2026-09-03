# Use official Python slim image as base
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Create a non-root user for security
RUN adduser --disabled-password --gecos '' appuser
EXPOSE 8000

# Copy dependency definitions
COPY pyproject.toml .
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -e .

# Copy the rest of the application
COPY src/ ./src/
COPY README.md .
COPY LICENSE .

# Change to non-root user
USER appuser

# The edupage-mcp-full console script is installed by the editable install above
# Alternatively, we can use: python -m edupage_mcp
ENTRYPOINT ["python", "-m", "edupage_mcp"]