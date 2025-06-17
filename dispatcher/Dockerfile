FROM python:3.9-slim

# Install supervisor
RUN apt-get update && apt-get install -y supervisor && apt-get clean

# Set working directory
WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source files
COPY src/ /app

# Expose ports (5001 for web UI)
EXPOSE 5001

# Create supervisor directory
RUN mkdir -p /etc/supervisor/conf.d

# Copy supervisor config
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf

# Run both processes
CMD ["/usr/bin/supervisord", "-n"]
