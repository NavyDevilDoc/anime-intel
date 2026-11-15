# Use the official Python 3.12 lightweight image
FROM python:3.12-slim

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code + data (FAISS index, metadata, SQLite DB)
COPY . .

# Railway assigns $PORT dynamically
EXPOSE ${PORT:-8000}

# Serve FastAPI via uvicorn. Single worker because FAISS index is
# loaded into process memory (~70MB). Multiple workers would each
# load a separate copy.
CMD uvicorn api.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1
