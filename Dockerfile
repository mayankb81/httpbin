FROM python:3.9-slim as builder

WORKDIR /app
COPY requirements.txt setup.py MANIFEST.in ./
COPY httpbin/ ./httpbin/

# 1. Install your clean, updated requirements first
RUN pip install --no-cache-dir -r requirements.txt

# 2. Install httpbin without letting setup.py try to download old dependencies
RUN pip install --no-cache-dir --no-deps .

FROM python:3.9-slim as runtime

LABEL name="httpbin"
LABEL version="0.9.2"
LABEL description="A simple HTTP service."
LABEL org.kennethreitz.vendor="Kenneth Reitz"

ENV LC_ALL=C.UTF-8
ENV LANG=C.UTF-8

RUN apt-get update -qq && apt-get install -y --no-install-recommends gcc git \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -r httpbin && useradd -r -g httpbin httpbin

WORKDIR /app
COPY --from=builder /usr/local/lib/python3.9/site-packages /usr/local/lib/python3.9/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY --from=builder /app /app

RUN pip install --no-cache-dir gunicorn==20.1.0 gevent==24.2.1

USER 1001
EXPOSE 80

CMD ["gunicorn", "-b", "0.0.0.0:80", "httpbin:app", "-k", "gevent"]
