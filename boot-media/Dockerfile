FROM python:3.14-slim-trixie
RUN apt-get update && apt-get install -y --no-install-recommends \
    bc bison build-essential ca-certificates curl device-tree-compiler \
    dosfstools flex gcc-aarch64-linux-gnu gcc-arm-linux-gnueabihf \
    libgnutls28-dev libssl-dev mtools pkg-config swig u-boot-tools \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /work
