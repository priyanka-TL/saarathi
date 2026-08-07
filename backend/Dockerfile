# The image Cloudflare Containers runs. Python 3.10 to match backend/.venv.
FROM python:3.10-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Dependencies first, so a code change does not reinstall them.
# psycopg[binary] and every other pin ship as wheels -- no compiler needed.
COPY requirements.txt constraints.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# There is deliberately NO .env in the image (.dockerignore excludes it).
# Real environment variables outrank .env in app/core/settings.py, and the
# container gets its whole configuration from the Worker's envVars/secrets.
#
# HOST must be 0.0.0.0, not the 127.0.0.1 default, or nothing outside the
# container can reach it. PORT must match `defaultPort` in cloudflare/index.ts.
ENV HOST=0.0.0.0 \
    PORT=8080 \
    WORKERS=1

EXPOSE 8080

# Mirrors `make run`: migrate first, then start. Re-running migrations on every
# boot is safe -- alembic no-ops when the schema is already at head.
CMD ["sh", "-c", "alembic upgrade head && python -m app.main"]
