FROM debian:trixie-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-yaml ca-certificates curl dnsmasq-base nfs-kernel-server \
    rpcbind util-linux e2fsprogs dosfstools rsync xz-utils kmod \
    qemu-user-static binfmt-support tini \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/pxe-fleet
COPY fleet ./fleet
COPY assets ./assets
ENTRYPOINT ["/usr/bin/tini", "-g", "--"]
CMD ["python3", "-m", "fleet.server"]
