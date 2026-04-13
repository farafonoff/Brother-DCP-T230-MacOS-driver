FROM --platform=linux/amd64 ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        ghostscript \
        file \
        perl \
        poppler-utils \
        xxd \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Drop in the whole Brother tree untouched.
COPY dcpt230 /opt/brother/Printers/dcpt230

# Symlink x86_64 binaries to the expected ${BR_PRT_PATH}/lpd/ location.
RUN ln -sf /opt/brother/Printers/dcpt230/lpd/x86_64/brdcpt230filter \
           /opt/brother/Printers/dcpt230/lpd/brdcpt230filter && \
    ln -sf /opt/brother/Printers/dcpt230/lpd/x86_64/brprintconf_dcpt230 \
           /opt/brother/Printers/dcpt230/lpd/brprintconf_dcpt230 && \
    chmod +x /opt/brother/Printers/dcpt230/lpd/filter_dcpt230

WORKDIR /work
COPY run.sh /work/run.sh
RUN chmod +x /work/run.sh

ENTRYPOINT ["/work/run.sh"]
