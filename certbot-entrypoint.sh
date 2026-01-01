#!/bin/sh
echo "dns_porkbun_key = $PORKBUN_KEY" > /porkbun.ini
echo "dns_porkbun_secret = $PORKBUN_SECRET" >> /porkbun.ini
chmod 600 /porkbun.ini

for DOMAIN in $(env | grep '^DOMAIN_' | cut -d= -f2); do
    if [ ! -d "/etc/letsencrypt/live/$DOMAIN" ]; then
        STAGING_FLAG=$([ "$STAGING" = "true" ] && echo "--staging" || echo "")
        certbot certonly --authenticator dns-porkbun \
            --dns-porkbun-credentials /porkbun.ini \
            --dns-porkbun-propagation-seconds 60 \
            $STAGING_FLAG -d "$DOMAIN" -d "*.$DOMAIN" \
            --email "$EMAIL" --agree-tos --no-eff-email --non-interactive
    fi
done

trap exit TERM; while :; do certbot renew --non-interactive; sleep 12h & wait ${!}; done;
