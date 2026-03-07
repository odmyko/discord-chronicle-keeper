FROM node:22-alpine

WORKDIR /app

COPY package.json ./
RUN npm install --omit=dev

COPY src ./src

ENV SIDECAR_PORT=8081
ENV DATA_DIR=/app/data

EXPOSE 8081

CMD ["npm", "start"]
