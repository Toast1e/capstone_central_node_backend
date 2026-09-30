#!/bin/sh
# Runs on every container start (the nginx image runs the scripts in
# /docker-entrypoint.d/ before NGINX). Serves HTTPS when a certificate is
# mounted at /etc/nginx/certs, plain HTTP otherwise. The PWA can only be
# installed to a phone's home screen over HTTPS.
set -e

if [ -f /etc/nginx/certs/fullchain.pem ] && [ -f /etc/nginx/certs/privkey.pem ]; then
    cp /etc/nginx/https.conf /etc/nginx/conf.d/default.conf
    echo "$0: certificate found, serving HTTPS on 443 (HTTP redirects to it)"
else
    cp /etc/nginx/http.conf /etc/nginx/conf.d/default.conf
    echo "$0: no certificate in /etc/nginx/certs, serving plain HTTP on 80"
fi
