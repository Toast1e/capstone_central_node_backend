FROM nginx:alpine

COPY default.conf /etc/nginx/conf.d/default.conf
COPY html/ /usr/share/nginx/html/

# Vendor Chart.js at build time so the dashboard is fully self-hosted and
# works even when the LAN has no internet access.
ADD https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js /usr/share/nginx/html/chart.umd.min.js
RUN chmod 644 /usr/share/nginx/html/chart.umd.min.js
