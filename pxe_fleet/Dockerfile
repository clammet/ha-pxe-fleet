FROM debian:trixie-slim AS bootloaders
RUN apt-get update && apt-get install -y --no-install-recommends \
    bc bison build-essential ca-certificates curl device-tree-compiler flex \
    gcc-aarch64-linux-gnu gcc-arm-linux-gnueabihf libgnutls28-dev libssl-dev \
    python3 pkg-config swig bzip2 && rm -rf /var/lib/apt/lists/*
COPY bootloader /source
WORKDIR /work
RUN bash /source/build-tools.sh all

FROM debian:trixie-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-yaml ca-certificates curl dnsmasq-base nfs-kernel-server \
    rpcbind util-linux e2fsprogs dosfstools mtools rsync xz-utils kmod \
    qemu-user-static binfmt-support tini \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/pxe-fleet
COPY fleet ./fleet
COPY assets ./assets
COPY bootloader/boot.cmd ./bootloader/boot.cmd
COPY --from=bootloaders /work/out/bin ./bootloaders
ENTRYPOINT ["/usr/bin/tini", "-g", "--"]
CMD ["python3", "-m", "fleet.server"]
