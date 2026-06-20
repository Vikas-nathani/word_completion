FROM python:3.10-slim

WORKDIR /app

# Copy files
COPY . .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Expose port
EXPOSE 8004

# Run app
CMD ["uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8004", "--workers", "4"]
