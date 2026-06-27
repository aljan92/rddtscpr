FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

WORKDIR /app

# System dependencies
RUN apt-get update && apt-get install -y \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Ensure Playwright dependencies are set up (Chromium is pre-installed in this image)
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# Copy code
COPY . .

# Expose port
EXPOSE 8000

# Start command
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
