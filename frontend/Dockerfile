ARG NODE_IMAGE=node:24-alpine
FROM ${NODE_IMAGE}

WORKDIR /app

COPY package*.json ./
RUN npm install

COPY . .

EXPOSE 5173
