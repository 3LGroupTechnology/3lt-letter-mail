# 3L-Group Technology — AI Letter Mail (production image)
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . ./

# Fail fast if the admin token is missing — never run production on the
# auto-generated dev token.
RUN chmod +x entrypoint.sh

EXPOSE 8000 8001

ENTRYPOINT ["/app/entrypoint.sh"]
