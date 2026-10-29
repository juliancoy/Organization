FROM quay.io/biocontainers/advntr@sha256:1d9c04c007c2d986ea988ed3eac6c61719bc489144db4fbc8105246d581fc35c
USER root
# pomegranate 0.6.1 uses NetworkX 1.x list-returning nodes() and graph.edge.
COPY requirements.txt /opt/dat1-requirements.txt
RUN python -m pip install --no-cache-dir --no-deps --require-hashes -r /opt/dat1-requirements.txt
COPY sitecustomize.py /opt/dat1-compat/sitecustomize.py
ENV PYTHONPATH=/opt/dat1-compat
# adVNTR uses Biopython's MUSCLE 3 command syntax; MUSCLE 5 is incompatible.
RUN python -c "import urllib.request,hashlib; b=urllib.request.urlopen('https://deb.debian.org/debian/pool/main/m/muscle/muscle_3.8.1551-2_amd64.deb').read(); assert hashlib.sha256(b).hexdigest() == '3195f0d29edab1f58dedee3e5ba87b4e56dfb3bacac405a47f51ae445c9fc89e'; open('/tmp/muscle3.deb','wb').write(b)" \
    && dpkg-deb -x /tmp/muscle3.deb /opt/muscle3 \
    && rm /tmp/muscle3.deb
ENV PATH=/opt/muscle3/usr/bin:${PATH}
# The upstream activation entry point resets PATH, selecting MUSCLE 5 again.
# All required paths are explicit; execute the requested command directly.
ENV LD_LIBRARY_PATH=/usr/local/lib
ENTRYPOINT []
