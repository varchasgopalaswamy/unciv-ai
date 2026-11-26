FROM docker.io/library/ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
ENV HOME=/tmp/home
RUN apt-get update && apt-get install -y --no-install-recommends \
    openjdk-21-jre-headless ca-certificates libstdc++6 libgcc-s1 \
    && rm -rf /var/lib/apt/lists/*
COPY lib /opt/unciv/lib
COPY assets /opt/unciv/assets
COPY provenance.json /opt/unciv/provenance.json
WORKDIR /opt/unciv/assets
ENTRYPOINT ["java", "-XX:MaxRAMPercentage=65", "-Djava.awt.headless=true", "-Duncivai.assetsDir=/opt/unciv/assets", "-cp", "/opt/unciv/lib/*", "uncivai.MainKt"]
