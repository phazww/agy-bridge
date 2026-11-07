FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY agy-bridge /usr/local/bin/agy-bridge
RUN chmod +x /usr/local/bin/agy-bridge && ln -s /usr/local/bin/agy-bridge /usr/local/bin/ab

ENTRYPOINT ["agy-bridge", "mcp"]
