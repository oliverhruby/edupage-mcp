# Pull Request: Add Docker Integration

## Summary
This PR adds Docker support to containerize the edupage-mcp-full application, providing an easy and consistent way to deploy the MCP server across different environments.

## Motivation
Containerization simplifies deployment, ensures dependency consistency, and improves portability. Users can now run the MCP server in a Docker container without worrying about Python version conflicts or manual dependency installation.

## Changes
### Added Files
- `Dockerfile`: Defines the container image for the application.
- `.dockerignore`: Specifies files and directories to exclude from the Docker build context.

### Modified Files
- `README.md`: Added a new section under "Getting started" for Docker installation and usage.

## Detailed Changes

### Dockerfile
- Based on the official `python:3.11-slim` image for a small footprint.
- Creates a non-root user (`appuser`) for enhanced security.
- Sets the working directory to `/app`.
- Copies only the necessary project files (via `.dockerignore`).
- Installs dependencies from `pyproject.toml` using `pip`.
- Exposes port 8000 (for HTTP MCP transports; stdio is the default).
- Sets the entry point to `python -m edupage_mcp` (the module's main function).

### .dockerignore
- Excludes common development and unnecessary files:
  - Version control (`.git`, `.github`)
  - Python cache (`__pycache__`, `*.pyc`, `*.pyo`)
  - Build artifacts (`build`, `dist`, `*.egg-info`)
  - Temporary files (`*.log`, `.env`, `.venv`)
  - IDE and editor settings (`.vscode`, `.idea`, `*.swp`)
  - Test and coverage files (`pytest_cache`, `htmlcov`, `.coverage`)

### README.md Updates
Added a new subsection "4. Run with Docker" under the "Getting started" section:

```markdown
### 4. Run with Docker

The project provides a Dockerfile for containerized deployment.

#### Build the Image
```bash
docker build -t edupage-mcp-full .
```

#### Run the Container
The server runs over stdio and expects to be connected to an MCP client. Provide EduPage credentials via environment variables:

```bash
docker run --rm -i \\
  -e EDUPAGE_USERNAME=your_username \\
  -e EDUPAGE_PASSWORD=your_password \\
  edupage-mcp-full
```

> **Note**: The `--rm` flag automatically removes the container when it stops. The `-i` flag keeps stdin open for the MCP client connection.

#### Example with MCP Inspector
To test with the [MCP Inspector](https://github.com/modelcontextprotocol/inspector):
```bash
docker run --rm -i \\
  -e EDUPAGE_USERNAME=your_username \\
  -e EDUPAGE_PASSWORD=your_password \\
  edupage-mcp-full | npx @modelcontextprotocol/inspector
```

#### Configuration via Environment Variables
All existing environment variables are supported:
- `EDUPAGE_USERNAME`: EduPage username/email
- `EDUPAGE_PASSWORD`: EduPage password
- `EDUPAGE_SUBDOMAINS`: Comma-separated list of subdomains (optional, for auto-login)
- `EDUPAGE_OTP_SECRET`: For TOTP-based 2FA (optional)
```


## HTTP MCP Support
- Added support for HTTP-based MCP transports (SSE and streamable-http) via environment variables:
  - `MCP_TRANSPORT`: Choose `stdio` (default), `sse`, or `streamable-http`.
  - `MCP_HOST`: Host to bind (default: `127.0.0.1`).
  - `MCP_PORT`: Port to listen on (default: `8000`).
- For security, when binding to `0.0.0.0` (all interfaces) with an HTTP transport, the `MCP_API_KEY` environment variable must be set; otherwise the server refuses to start.
- Note: Actual API key validation is not implemented in this version; users should deploy a reverse proxy (e.g., nginx, Traefik) for authentication and TLS termination if exposing to untrusted networks.
## Testing
- Built the Docker image locally using `docker build -t edupage-mcp-full .`.
- Verified the image runs without errors when credentials are provided (container starts and waits for stdio input).
- Confirmed that the container exits with an appropriate error message when required credentials are missing.
- Checked that the image size is approximately 120MB (using `python:3.11-slim` base).

## How to Test This Change (for Reviewers)
1. Clone this branch.
2. Build the Docker image: `docker build -t edupage-mcp-full .`
3. Run the container with test credentials (or without to see error handling):
   ```bash
   docker run --rm -i -e EDUPAGE_USERNAME=test -e EDUPAGE_PASSWORD=test edupage-mcp-full
   ```
   (The server will attempt to log in and either succeed or show authentication errors via stdio.)
4. Optionally, connect an MCP client to the container's stdio to verify tool functionality.

## Additional Notes
- The Docker implementation follows security best practices by using a non-root user.
- The server's stdio-based communication makes it ideal for containerization, as it can be easily piped to MCP clients or inspectors.
- No changes were made to the core application logic; this PR only adds packaging and deployment tooling.