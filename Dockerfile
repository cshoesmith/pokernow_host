FROM python:3.11-slim

# Install Chrome for the Selenium scraper
RUN apt-get update && apt-get install -y \
    wget gnupg ca-certificates curl \
    && wget -q -O - https://dl-ssl.google.com/linux/linux_signing_key.pub | apt-key add - \
    && echo "deb [arch=amd64] http://dl.google.com/linux/chrome/deb/ stable main" \
       > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update && apt-get install -y \
       google-chrome-stable \
       --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# data/ is mounted as a Fly volume; create it if absent so local runs still work
RUN mkdir -p /app/data

ENV HOST=0.0.0.0
ENV PORT=8080

CMD ["python", "run.py"]
