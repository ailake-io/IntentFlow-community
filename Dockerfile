FROM ghcr.io/astral-sh/uv:0.9.18@sha256:5713fa8217f92b80223bc83aac7db36ec80a84437dbc0d04bbc659cae030d8c9 AS uv-bin

FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
WORKDIR /app
COPY --from=uv-bin /uv /uvx /bin/
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --locked --no-dev --extra secrets --extra postgres \
    && groupadd --gid 10001 intentflow \
    && useradd --uid 10001 --gid 10001 --create-home intentflow \
    && mkdir -p /data \
    && chown intentflow:intentflow /data
ENV INTENTFLOW_STORE=/data/intentflow.db
ENV PATH="/app/.venv/bin:${PATH}"
USER 10001:10001
VOLUME /data
EXPOSE 8787
# Exposure outside loopback requires users and TLS/proxy; see docs/DEPLOYMENT.md.
ENTRYPOINT ["intentflow"]
CMD ["ui"]
