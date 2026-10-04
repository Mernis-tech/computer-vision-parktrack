#!/usr/bin/env bash

WORKERS_COUNT="${CAR_DETECTOR_WORKERS_COUNT:-1}"
LOOP_DELAY="${CAR_DETECTOR_LOOP_DELAY_SECONDS:-60}"

if ! [[ "$WORKERS_COUNT" =~ ^[1-9][0-9]*$ ]]; then
    echo "CAR_DETECTOR_WORKERS_COUNT must be a positive integer"
    exit 1
fi

worker_loop() {
    local worker_id="$1"

    while true; do
        echo "Starting detection cycle in worker ${worker_id}..."

        python -m detection.main \
            --model detection/best_openvino_model/best.xml \
            --base-api-url "$API_URL" \
            --api-token "$API_TOKEN" \
            --device "CPU"

        echo "Worker ${worker_id} finished cycle. Sleeping ${LOOP_DELAY}s..."
        sleep "$LOOP_DELAY"
    done
}

pids=()

stop_workers() {
    echo "Stopping workers..."

    for pid in "${pids[@]}"; do
        kill "$pid" 2>/dev/null || true
    done

    wait
    exit 0
}

trap stop_workers SIGTERM SIGINT

echo "Starting ${WORKERS_COUNT} car-detector worker(s)..."

for i in $(seq 1 "$WORKERS_COUNT"); do
    worker_loop "$i" &
    pids+=("$!")
done

wait
