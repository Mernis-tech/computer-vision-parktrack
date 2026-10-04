FROM python:3.10-slim

WORKDIR /app

# Install system dependencies for OpenCV/OpenVINO + bash for run_loop.sh
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY detection/ ./detection/
COPY run_loop.sh .

RUN chmod +x ./run_loop.sh

ENV PYTHONPATH="/app/detection:${PYTHONPATH}"

CMD ["./run_loop.sh"]
