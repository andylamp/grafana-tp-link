#!/bin/bash

# This is script stops, removes, and prunes the containers created using the grafana-tp-link-docker script
# from the system. Note: as it's evident from the shebang it was developed
# (and tested) with bash (only).
#
# Author: Andreas Grammenos (ag926@cl.cam.ac.uk)
#
# Last touched: 05/09/2020
#

# the grafana dockerfile name - this has to be present!
GRAFANA_DOCKERFILE=grafana.yaml
# the project name
DOCK_PROJECT_NAME="grafana-tp-link"

docker-compose -p ${DOCK_PROJECT_NAME} -f ${GRAFANA_DOCKERFILE} down &&
  docker container prune -f &&
  # remove the data stored by grafana and prometheus - you might need to change these
  sudo rm -rf /usr/grafana-container-data/ /usr/prometheus-container-data/
