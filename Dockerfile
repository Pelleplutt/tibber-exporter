FROM python:3.11.2

RUN addgroup prometheus
RUN adduser --disabled-password --no-create-home --home /app  --gecos '' --ingroup prometheus prometheus

COPY requirements.txt /app/
RUN /usr/local/bin/pip install -r /app/requirements.txt

COPY tibber-exporter.py /app/

EXPOSE 9109

CMD ["/usr/local/bin/python",  "/app/tibber-exporter.py"]
