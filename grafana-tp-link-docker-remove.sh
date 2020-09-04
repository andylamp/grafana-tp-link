#!/bin/bash

# This is script stops, removes, and prunes the containers created using the grafana-tp-link-docker script
# from the system

# the grafana dockerfile name - this has to be present!
GRAFANA_DOCKERFILE=grafana.yaml
# the project name
DOCK_PROJECT_NAME="grafana-tp-link"

docker-compose -p ${DOCK_PROJECT_NAME} -f ${GRAFANA_DOCKERFILE} down && \
docker container prune -f && \
# remove the data stored by grafana and prometheus - you might need to change these
sudo rm -rf /usr/grafana-container-data/ /usr/prometheus-container-data/