# Den agent image: one container per agent, Codex runs only via `docker exec`.
#
# Built by the backend on first start (DockerRuntime.prepare) as
# den-agent:0.163.0-2, or by hand:
#   docker build -t den-agent:0.163.0-2 container
FROM node:22-slim

# ca-certificates is required: without it Codex's model requests never
# complete and turns hang on "Reconnecting... waiting for network".
# The rest are tools a general-purpose assistant commonly reaches for.
# python-is-python3 makes `python` work: models often try it first, and
# without it every such command fails with exit 127.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates git python3 python-is-python3 python3-pip python3-venv python3-requests curl procps less \
    && rm -rf /var/lib/apt/lists/*

# Codex CLI pinned to 0.163.0, the version bundled with the Python SDK
# (openai-codex 0.163.0) that the test suite runs; the SDK speaks exactly
# this app-server protocol, and nested MCP tool calls were verified on it.
RUN npm install -g @openai/codex@0.163.0 \
    && npm cache clean --force

# Containers run as the backend's host uid:gid (`docker run --user`), which
# has no entry or home in this image. World-writable (sticky) directories
# let any uid use HOME, and /workspace and /codex are bind-mounted over at
# run time anyway.
RUN mkdir -p /workspace /codex /home/agent \
    && chmod 1777 /home/agent /codex /workspace

ENV CODEX_HOME=/codex HOME=/home/agent
WORKDIR /workspace

# The main process only keeps the container alive; each turn starts its own
# `codex app-server` with `docker exec -i`.
CMD ["sleep", "infinity"]
