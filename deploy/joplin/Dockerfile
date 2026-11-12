# media-library's headless Joplin client (D73): the Joplin CLI with every dependency locked (package-lock.json),
# and the two scripts that drive it (client.sh, lists.mjs). Node 22 for node:sqlite. Built by deploy/joplin/compose.yml.
# JOPLIN_VERSION other than the locked one installs that version unlocked (see docs/INSTALL.md §6d: never newer
# than what your Joplin apps accept).
FROM node:22-bookworm-slim AS build
ARG JOPLIN_VERSION=3.7.1
RUN apt-get update && apt-get install -y --no-install-recommends python3 make g++ && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/medialib-joplin
COPY package.json package-lock.json ./
RUN if [ "$JOPLIN_VERSION" = "$(node -p 'require("./package.json").dependencies.joplin')" ]; then \
      npm ci --omit=dev --no-audit --no-fund; \
    else \
      npm install --omit=dev --no-audit --no-fund --no-save "joplin@$JOPLIN_VERSION"; \
    fi && npm cache clean --force

FROM node:22-bookworm-slim
COPY --from=build /opt/medialib-joplin/node_modules /opt/medialib-joplin/node_modules
COPY client.sh lists.mjs /opt/medialib-joplin/
ENV PATH=/opt/medialib-joplin/node_modules/.bin:$PATH NODE_ENV=production HOME=/tmp
USER node
ENTRYPOINT ["/opt/medialib-joplin/client.sh"]
