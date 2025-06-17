FROM python:3.9-slim

# Set working directory
WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy crawler script and related files
COPY src/ /app

# Run the crawler script
CMD ["python", "crawler.py"]
