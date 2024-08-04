FROM golang:1.21

WORKDIR /app

COPY . .

RUN go mod download

RUN go build -o search .

EXPOSE 8080

CMD ["/app/search", "-prefork=false", "-dev=false", "-port=:8080"]

