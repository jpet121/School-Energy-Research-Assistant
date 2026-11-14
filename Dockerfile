FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update \
    && apt-get install --no-install-recommends -y libseccomp2 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --create-home retrofit \
    && install -d -m 0700 -o retrofit /data
WORKDIR /opt/retrofit-evidence
COPY deploy/runtime.lock /tmp/runtime.lock
RUN python -m pip install --no-cache-dir --only-binary=:all: -r /tmp/runtime.lock \
    && python -m pip check
COPY retrofit ./retrofit
COPY deploy/gateway.py ./gateway.py
USER 10001
VOLUME ["/data"]
EXPOSE 8765
ENTRYPOINT ["python", "gateway.py"]
