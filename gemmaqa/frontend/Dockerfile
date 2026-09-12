# Local compose only (Vite dev server). Production Cloud Run uses the
# repository-root Dockerfile, which runs `npm run build` and serves dist/
# from FastAPI. Do not use this image on Cloud Run.
FROM node:20-alpine

WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm install
COPY . .
EXPOSE 5173
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]
