FROM python:3.7-slim

LABEL name="httpbin"
LABEL version="0.9.2"
LABEL description="A simple HTTP service."
LABEL org.kennethreitz.vendor="Kenneth Reitz"

ENV LC_ALL=C.UTF-8
ENV LANG=C.UTF-8

RUN apt-get update -qq && apt-get install -y --no-install-recommends gcc git \
    && rm -rf /var/lib/apt/lists/*

ADD . /httpbin
WORKDIR /httpbin
RUN pip install --no-cache-dir -c constraints.txt /httpbin gunicorn==20.1.0

EXPOSE 80

CMD ["gunicorn", "-b", "0.0.0.0:80", "httpbin:app", "-k", "gevent"]
