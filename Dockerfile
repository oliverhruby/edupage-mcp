FROM python:3.11-alpine

LABEL io.modelcontextprotocol.server.name="io.github.oliverhruby/edupage-mcp"

RUN apk upgrade --no-cache

WORKDIR /app

RUN adduser -D appuser

COPY pyproject.toml README.md LICENSE ./
COPY src/ ./src/

# The container only ever runs `python -m edupage_mcp`; it never installs
# anything. So pip is removed once the install is done. That is not just size:
# pip vendors its own urllib3 (26.2.1 vendors 2.7.0) and Trivy reports vendored
# packages, so shipping pip reports CVE-2026-97687 / CVE-2026-97689 against a
# copy of urllib3 the server never reaches. Our own traffic goes through
# requests -> the real urllib3, which pyproject.toml floors at 2.8.0. No pip
# release vendors urllib3 >= 2.8.0, so this is the fix rather than a
# suppression. Removing the base image's own pip at the same time is why the
# globs cover plain names too, not just versioned dist-infos.
RUN pip install --no-cache-dir --upgrade "pip>=26.2.0" "setuptools>=83.0.0" "wheel>=0.46.3" \
    && pip install --no-cache-dir --no-build-isolation . \
    && rm -rf /usr/local/lib/python3.11/site-packages/pip \
              /usr/local/lib/python3.11/site-packages/pip-*.dist-info \
              /usr/local/lib/python3.11/site-packages/setuptools \
              /usr/local/lib/python3.11/site-packages/setuptools-*.dist-info \
              /usr/local/lib/python3.11/site-packages/pkg_resources \
              /usr/local/lib/python3.11/site-packages/_distutils_hack \
              /usr/local/lib/python3.11/site-packages/distutils-precedence.pth \
              /usr/local/lib/python3.11/site-packages/wheel \
              /usr/local/lib/python3.11/site-packages/wheel-*.dist-info \
              /usr/local/lib/python3.11/site-packages/ensurepip \
    && rm -f /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.11 /usr/local/bin/wheel \
    && python -c "import edupage_mcp, edupage_api, mcp.server.fastmcp"

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,socket,sys; t=os.getenv('MCP_TRANSPORT','stdio').lower(); p=int(os.getenv('MCP_PORT','8000')); sys.exit(0) if t=='stdio' else socket.create_connection(('127.0.0.1', p), 2).close()"

USER appuser

ENTRYPOINT ["python", "-m", "edupage_mcp"]
