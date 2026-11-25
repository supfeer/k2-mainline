FROM alpine:3.20

RUN apk add --no-cache squashfs-tools python3

WORKDIR /work
