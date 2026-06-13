FROM python:3.12-slim

WORKDIR /app

# Install only the BCF/GraphQL API dependencies (no ifcopenshell, no dashboard)
COPY requirements-deploy.txt .
RUN pip install --no-cache-dir -r requirements-deploy.txt

# Copy application source
COPY . .

# Render injects $PORT at runtime; default to 8000 for local docker runs
EXPOSE 8000
CMD uvicorn main:app --host 0.0.0.0 --port "${PORT:-8000}"
