# Use an official Python runtime as a parent image
FROM python:3.12-slim

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file into the container at /app
COPY requirements.txt .

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code into the container at /app
COPY . .

# Log lines appear right away in `docker logs` / `kubectl logs`
ENV PYTHONUNBUFFERED=1

# Spotify login callback listener (api.callbackPort); only listens while waiting for a login
EXPOSE 9123

# Define the command to run the application
CMD ["python", "PlaylistGenerator.py"]