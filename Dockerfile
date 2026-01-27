# Use Alpine Linux as base image
FROM alpine:3.23

# Install Python3 and HackRF tools from Alpine package repositories
RUN apk add --no-cache \
    python3 \
    hackrf

# Create application directory
WORKDIR /app

# Copy the Python script into the container
COPY hackrf_spectrum_monitor.py .

# Make the script executable
RUN chmod +x hackrf_spectrum_monitor.py

# Set default VictoriaMetrics URL (can be overridden at runtime with -e VM_URL=...)
ENV VM_URL=http://localhost:8428

# Run the script by default with environment variable substitution
# Using exec form with sh -c for secure environment variable expansion
CMD ["sh", "-c", "exec python3 hackrf_spectrum_monitor.py --vm-url \"$VM_URL\" --batch-interval 0.5"]
