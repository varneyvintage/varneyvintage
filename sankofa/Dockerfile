FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY sankofa ./sankofa
RUN pip install --no-cache-dir -e ".[webhook]"
COPY config ./config
# state/ is mounted as a volume so paper account, journal and leader scores survive restarts
VOLUME ["/app/state"]
ENV SANKOFA_CONFIG=config/settings.yaml
EXPOSE 8787
CMD ["sankofa", "run"]
