FROM node:22-alpine AS build
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build
FROM nginx:1.27-alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY deploy/arm2-windows-uat/nginx.conf /etc/nginx/conf.d/default.conf
COPY deploy/arm2-windows-uat/instance.json /usr/share/nginx/html/uat-instance.json
LABEL io.arm2.uat.package="ARM2-WIN-UAT-R2-001" io.arm2.uat.source="1f8c06d1f9269146ce7c07508b02dd09c89ff763"
