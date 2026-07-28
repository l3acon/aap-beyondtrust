# Custom AAP controller image with BeyondTrust Password Safe credential plugin.
#
# Build:
#   podman build -t registry.example.com/aap/controller-beyondtrust:latest .
#
# The BASE_IMAGE should match the controller image running in your AAP 2.7
# containerized deployment. Find it with:
#   podman ps --format '{{.Image}}' | grep controller

ARG BASE_IMAGE=quay.io/aap/ansible-automation-platform-27-next/controller-rhel9:latest
FROM ${BASE_IMAGE}

USER root

COPY . /tmp/beyondtrust-plugin/

RUN /var/lib/awx/venv/awx/bin/pip install --no-cache-dir /tmp/beyondtrust-plugin/ && \
    rm -rf /tmp/beyondtrust-plugin/

USER 1000
