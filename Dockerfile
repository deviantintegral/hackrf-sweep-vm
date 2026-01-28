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

# Set default environment variables (can be overridden at runtime with -e)
ENV VM_URL=http://localhost:8428
ENV BATCH_INTERVAL=0.5
ENV LNA_GAIN=32
ENV VGA_GAIN=20
ENV BIN_WIDTH=1000000
ENV FREQUENCY_RANGE=2400:2485

# Run the script by default with environment variable substitution
# Using exec form with sh -c for secure environment variable expansion
CMD ["sh", "-c", "exec python3 hackrf_spectrum_monitor.py --vm-url \"$VM_URL\" --batch-interval \"$BATCH_INTERVAL\" --lna-gain \"$LNA_GAIN\" --vga-gain \"$VGA_GAIN\" --bin-width \"$BIN_WIDTH\" --frequency-range \"$FREQUENCY_RANGE\""]
