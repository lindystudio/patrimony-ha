ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-python:3.12-alpine3.20
FROM ${BUILD_FROM}
WORKDIR /app
COPY web.py /app/web.py
COPY static /app/static
COPY seed_demo.json /app/seed_demo.json
COPY default.jpg /app/default.jpg
CMD ["python3", "/app/web.py"]
