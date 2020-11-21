FROM python:3.7

RUN addgroup prometheus
RUN adduser --disabled-password --no-create-home --home /app  --gecos '' --ingroup prometheus prometheus

COPY requirements.txt /app/
RUN /usr/local/bin/pip3.7 install -r /app/requirements.txt

COPY tibber-exporter.py /app/
COPY graphql_client.py /usr/local/lib/python3.7/site-packages/python_graphql_client/graphql_client.py


EXPOSE 9109

CMD ["/usr/local/bin/python",  "/app/tibber-exporter.py"]
