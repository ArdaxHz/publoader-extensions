FROM python:3.10-bullseye

WORKDIR /extensions

COPY . .

RUN find . -type f -name "requirements.txt" -exec pip install --no-cache-dir -r {} \;

CMD ["python", "sync_extensions.py"]
