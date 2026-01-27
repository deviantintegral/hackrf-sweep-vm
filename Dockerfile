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

# Run the script by default
ENTRYPOINT ["python3", "hackrf_spectrum_monitor.py"]

# Default arguments (can be overridden at runtime)
CMD ["--vm-url", "http://localhost:8428", "--batch-interval", "0.5"]
