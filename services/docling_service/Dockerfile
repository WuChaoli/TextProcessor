ARG DOCLING_BASE_IMAGE=quay.io/docling-project/docling-serve-cpu:v1.28.0@sha256:cc207e1eb768878456ed98042c5d84fae56af3729a9c03d3e5c8fef393902956
FROM ${DOCLING_BASE_IMAGE}

USER root
RUN mkdir -p /opt/textprocessor-docling /run/textprocessor-docling \
    && chown -R 1001:0 /opt/textprocessor-docling /run/textprocessor-docling

COPY --chown=1001:0 --chmod=0555 services/docling_service/process_manager.py /opt/textprocessor-docling/process_manager.py
COPY --chown=1001:0 --chmod=0555 services/docling_service/healthcheck.py /opt/textprocessor-docling/healthcheck.py

USER 1001:0
ENTRYPOINT ["python", "/opt/textprocessor-docling/process_manager.py"]
CMD []
