FROM arm32v7/python:3.9.12-buster

ENV DEBIAN_FRONTEND=noninteractive
RUN python3 -m pip install --no-cache-dir \
        cffi==1.14.6 \
        pyserial==3.4 \
        greenlet==1.0.0 \
        Jinja2==2.11.3 \
        markupsafe==1.1.1 \
        python-can==3.3.4

WORKDIR /work
