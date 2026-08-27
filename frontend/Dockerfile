# Dev-mode Dockerfile: runs the Vite dev server (docker-compose target).
# A production nginx-based build (port 8000, per architecture.md §1.1) is
# added in Phase 8 alongside the real review UI — nothing to serve yet.
FROM node:22-alpine

RUN apk add --no-cache curl

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

COPY . .

EXPOSE 5173
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]
