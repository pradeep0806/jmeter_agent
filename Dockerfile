FROM python:3.11-slim

ARG JMETER_VERSION=5.6.3
ARG PROMETHEUS_PLUGIN_VERSION=0.9.0
ENV JMETER_HOME=/opt/apache-jmeter-${JMETER_VERSION}
ENV PATH=${JMETER_HOME}/bin:${PATH}

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        openjdk-17-jre-headless \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Apache JMeter (CLI mode only — no GUI dependencies needed)
RUN curl -fsSL "https://archive.apache.org/dist/jmeter/binaries/apache-jmeter-${JMETER_VERSION}.tgz" \
        -o /tmp/jmeter.tgz \
    && tar -xzf /tmp/jmeter.tgz -C /opt \
    && rm /tmp/jmeter.tgz \
    && ln -s ${JMETER_HOME}/bin/jmeter /usr/local/bin/jmeter

# jmeter-prometheus-plugin (johrstrom) backend listener, for the live Grafana view
RUN curl -fsSL \
        "https://github.com/johrstrom/jmeter-prometheus-plugin/releases/download/${PROMETHEUS_PLUGIN_VERSION}/jmeter-prometheus-plugin-${PROMETHEUS_PLUGIN_VERSION}.jar" \
        -o "${JMETER_HOME}/lib/ext/jmeter-prometheus-plugin.jar" \
    && curl -fsSL \
        "https://repo1.maven.org/maven2/io/prometheus/simpleclient/0.16.0/simpleclient-0.16.0.jar" \
        -o "${JMETER_HOME}/lib/simpleclient.jar" \
    && curl -fsSL \
        "https://repo1.maven.org/maven2/io/prometheus/simpleclient_httpserver/0.16.0/simpleclient_httpserver-0.16.0.jar" \
        -o "${JMETER_HOME}/lib/simpleclient_httpserver.jar" \
    && curl -fsSL \
        "https://repo1.maven.org/maven2/io/prometheus/simpleclient_common/0.16.0/simpleclient_common-0.16.0.jar" \
        -o "${JMETER_HOME}/lib/simpleclient_common.jar"

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 9270

CMD ["python", "orchestrator.py"]
