# Use the official Python 3.9 image as the base image
FROM python:3.9-slim

# Set a working directory inside the container
WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the entire src folder into the container's /app directory
COPY src /app

# Expose the port on which the Flask app will run (default: 5000)
EXPOSE 5000

# Run the Flask app
CMD ["python", "app.py"]
