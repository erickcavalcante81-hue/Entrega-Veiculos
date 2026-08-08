FROM python:3.11-slim

WORKDIR /app

# Dependências do sistema
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \
    curl ffmpeg && rm -rf /var/lib/apt/lists/*

# Dependências Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Código do agente
COPY . .
COPY ../integrations /app/integrations

EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
  CMD curl -sf http://localhost:3000/health || exit 1

CMD ["python", "-m", "uvicorn", "joao_holanda_service:app", \
     "--host", "0.0.0.0", "--port", "3000", "--log-level", "info"]
