FROM mcr.microsoft.com/playwright/python:v1.61.0-noble

RUN pip install --no-cache-dir playwright==1.61.0 fastapi==0.115.12 uvicorn==0.34.2 mss==10.1.0 \
    && python -m playwright install chrome \
    && apt-get update \
    && apt-get install -y --no-install-recommends openbox x11vnc xdotool x11-utils novnc websockify \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 browser

WORKDIR /opt/desktop
COPY start.sh server.py url_policy.py ./
RUN chmod 755 start.sh && chmod 644 server.py url_policy.py
USER 10001:10001
ENV HOME=/tmp DISPLAY=:99 PYTHONUNBUFFERED=1
EXPOSE 8765 6080
CMD ["/opt/desktop/start.sh"]
