FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

# Dependencies first so a code change does not invalidate the layer.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Non-root. The app writes only to /app/data and /app/logs.
RUN useradd --create-home --uid 10001 agent \
 && mkdir -p /app/data /app/logs \
 && chown -R agent:agent /app/data /app/logs
USER agent

# The health check runs the component suite: a container that starts but whose
# controls are broken is not healthy, it is dangerous.
HEALTHCHECK --interval=60s --timeout=30s --start-period=10s --retries=2 \
  CMD python scripts/self_test.py > /dev/null 2>&1 || exit 1

CMD ["python", "run.py", "demo"]
