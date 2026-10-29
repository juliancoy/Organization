FROM quay.io/biocontainers/advntr@sha256:1d9c04c007c2d986ea988ed3eac6c61719bc489144db4fbc8105246d581fc35c
USER root
COPY requirements.txt /opt/dat1-scan-requirements.txt
RUN python -m pip install --no-cache-dir --no-deps --require-hashes -r /opt/dat1-scan-requirements.txt
ENV LD_LIBRARY_PATH=/usr/local/lib
ENTRYPOINT []
