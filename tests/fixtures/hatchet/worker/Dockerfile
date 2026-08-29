FROM python:3.12-slim

RUN pip install --no-cache-dir "hatchet-sdk==1.38.1"

WORKDIR /worker
COPY worker.py /worker/worker.py

CMD ["python", "/worker/worker.py"]
