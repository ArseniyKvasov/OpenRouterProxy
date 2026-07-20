#!/bin/sh
set -eu

: "${DOMAIN:?DOMAIN is required}"
: "${LETSENCRYPT_EMAIL:?LETSENCRYPT_EMAIL is required}"
: "${UPSTREAM_HOST:?UPSTREAM_HOST is required}"
: "${UPSTREAM_PORT:?UPSTREAM_PORT is required}"

CERT_DIR="/etc/letsencrypt/live/${DOMAIN}"

mkdir -p /var/www/certbot

envsubst '${DOMAIN} ${UPSTREAM_HOST} ${UPSTREAM_PORT}' \
    < /etc/nginx/templates/default.conf.template \
    | sed 's/\$\$/\$/g' \
    > /etc/nginx/conf.d/default.conf

if [ ! -f "${CERT_DIR}/fullchain.pem" ] || [ ! -f "${CERT_DIR}/privkey.pem" ]; then
    mkdir -p "${CERT_DIR}"
    openssl req -x509 -nodes -newkey rsa:2048 -days 1 \
        -keyout "${CERT_DIR}/privkey.pem" \
        -out "${CERT_DIR}/fullchain.pem" \
        -subj "/CN=${DOMAIN}" >/dev/null 2>&1
fi

nginx -g 'daemon off;' &
NGINX_PID=$!

issue_certificate() {
    certbot certonly \
        --webroot \
        --webroot-path /var/www/certbot \
        --email "${LETSENCRYPT_EMAIL}" \
        --agree-tos \
        --no-eff-email \
        --non-interactive \
        --keep-until-expiring \
        --domain "${DOMAIN}" || return 0

    nginx -s reload
}

issue_certificate

while :; do
    sleep 12h
    certbot renew --quiet && nginx -s reload || true
done &

wait "${NGINX_PID}"
