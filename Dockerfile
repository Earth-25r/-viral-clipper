

FROM python:3.11-slim

RUN apt-get update -y
RUN apt-get install -y ffmpeg
RUN apt-get install -y curl ca-certificates fonts-liberation

RUN useradd -m -u 1000 clipper
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /tmp/viral-clipper/outputs
RUN chown -R clipper:clipper /tmp/viral-clipper /app

USER clipper

EXPOSE 8000

CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1
