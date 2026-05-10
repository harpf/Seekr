#!/bin/sh
set -eu
DOMAIN="${TLS_DOMAIN:-Seekr.domain.tld}"
IP="${TLS_IP:-127.0.0.1}"
mkdir -p /certs
if [ ! -f /certs/tls.crt ] || [ ! -f /certs/tls.key ]; then
  cat >/tmp/openssl.cnf <<EOF
[req]
distinguished_name=req
x509_extensions = v3_req
prompt = no
[req_distinguished_name]
CN=${DOMAIN}
[v3_req]
subjectAltName=@alt_names
[alt_names]
DNS.1=${DOMAIN}
IP.1=${IP}
EOF
  openssl req -x509 -nodes -newkey rsa:2048 -days 825 -keyout /certs/tls.key -out /certs/tls.crt -config /tmp/openssl.cnf -subj "/CN=${DOMAIN}"
fi
nginx -g 'daemon off;'
