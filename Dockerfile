FROM python:3.10-bullseye

WORKDIR /extensions

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "sync_extensions.py"]
