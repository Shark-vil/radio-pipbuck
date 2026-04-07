FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY radio.py .
COPY templates/ ./templates/

EXPOSE 8000

ENV MUSIC_DIR=/music
ENV PORT=8000

CMD ["python", "radio.py"]