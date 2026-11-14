FROM python:3.12-alpine3.24@sha256:c5ba3deef00c7758561a0f03c26f44f10589263d40e732b8748e0d1f38f6dd89

# Fixed upstream Kerberos runtime package; refresh alongside the base digest.
RUN apk add --no-cache --upgrade krb5-libs=1.22.3-r0

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --only-binary=:all: -r requirements.txt

COPY *.py ./

USER 10001:10001
EXPOSE 8000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
