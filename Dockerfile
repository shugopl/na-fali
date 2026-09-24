# Aplikacja to czysta biblioteka standardowa Pythona — brak etapu budowania
# i brak zaleznosci do zainstalowania.
FROM python:3.12-alpine

RUN addgroup -g 10001 -S nafali && adduser -u 10001 -S -G nafali nafali

WORKDIR /app
COPY server.py exam_validation.py ./
COPY web/ ./web/

USER 10001:10001
ENV HOST=0.0.0.0 PORT=8080 DB_PATH=/data/course.sqlite3
EXPOSE 8080
CMD ["python3", "-u", "server.py"]
