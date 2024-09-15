FROM python:3.10-slim

WORKDIR /app

COPY . /app

RUN pip install -r requirements.txt

WORKDIR /app/web

EXPOSE 8000

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000" ]