FROM mcr.microsoft.com/playwright/python:v1.63.0-noble

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        fluxbox \
        novnc \
        socat \
        websockify \
        x11vnc \
        xvfb \
    && rm -rf /var/lib/apt/lists/*

COPY docker/browser/entrypoint.sh /usr/local/bin/browser-entrypoint
RUN chmod +x /usr/local/bin/browser-entrypoint \
    && mkdir -p /profile

EXPOSE 6080 9223

ENTRYPOINT ["/usr/local/bin/browser-entrypoint"]
