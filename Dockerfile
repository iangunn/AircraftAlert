# Use the official Python image as a base image
FROM python:3.13-slim

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file into the container
COPY requirements.txt ./

# Install the dependencies
RUN pip install --no-cache-dir --upgrade pip 
RUN pip list --outdated --format=freeze | cut -d '=' -f 1 | xargs -n1 pip install --no-cache-dir --upgrade
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code into the container
COPY . .

# Set environment variables
ENV PYTHONUNBUFFERED=1

# Set default value for RADIUS if not provided
ENV RADIUS=${RADIUS:-20}

# Define the command to run the application
CMD ["python", "aircraft_alert.py", "$POSTCODE", "-r", "$RADIUS"]
