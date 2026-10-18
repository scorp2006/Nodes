# Nodes server image. Base image pinned by digest (python:3.12-slim, pulled 2026-10-18).
FROM python@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
# Demo workspace: pallets/click at a fixed tag (BSD-3-Clause), used when no repo is mounted
RUN git clone --depth 1 --branch 8.1.7 https://github.com/pallets/click /demo/click
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY coop/world_graph_v2.py coop/
COPY nodes/ nodes/
EXPOSE 8780 8781
# NODES_PACKAGE set: graph the mounted repo (/repo/<package>); otherwise graph the click demo
CMD ["sh", "-c", "if [ -n \"$NODES_PACKAGE\" ]; then W=/repo; P=/repo/$NODES_PACKAGE; else W=/demo/click; P=/demo/click/src/click; fi; exec python -m nodes up --workspace \"$W\" --package \"$P\" --db-host falkordb --host 0.0.0.0 --log-dir /tmp/nodes"]
