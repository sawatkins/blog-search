# Use an official Python runtime as the base image
FROM python:3.10-slim

# Set the working directory in the container
WORKDIR /app

# Copy the current directory contents into the container at /app
COPY web-scraper/pages.db web-scraper/requirements.txt web-scraper/server.py web-scraper/search_engine.py web-scraper/templates /app/

# Install any needed packages specified in requirements.txt
# (You'll need to create this file with your project dependencies)

RUN pip install -r requirements.txt

# Make port 8000 available to the world outside this container
EXPOSE 8000

# Run server.py when the container launches
CMD ["python", "server.py"]