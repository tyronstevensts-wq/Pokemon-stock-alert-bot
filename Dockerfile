FROM mcr.microsoft.com/playwright/python

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN python -m playwright install --with-deps chromium

COPY . .

CMD ["python", "monitor.py"]
