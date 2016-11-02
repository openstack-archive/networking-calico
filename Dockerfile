FROM python:2.7

RUN pip install \
    virtualenv \
    tox \
    coverage \
    pytest

COPY requirements.txt /tmp/requirements.txt
COPY _test-requirements.txt /tmp/test-requirements.txt

RUN pip install -r /tmp/requirements.txt -r /tmp/test-requirements.txt
