FROM --platform=linux/amd64 python:3.12-slim

ENV PYTHONNOUSERSITE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ src/
COPY prompts/ prompts/
COPY agent.yaml .

RUN useradd -m -u 1000 appuser
USER appuser

EXPOSE 8088
CMD ["python", "-m", "src.main"]
