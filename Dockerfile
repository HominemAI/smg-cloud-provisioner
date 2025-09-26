FROM python:3.11.13-slim-bookworm

WORKDIR /app

# Apply OS patches
RUN apt-get update \
 && apt-get -y upgrade \
 && apt-get -y dist-upgrade \
 && apt-get autoremove -y \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

# Copy project files
COPY . .

# Install dependencies
RUN pip install --no-cache-dir --prefer-binary -r requirements.txt

# Run FastAPI app with uvicorn
CMD ["python3", "main:main"]